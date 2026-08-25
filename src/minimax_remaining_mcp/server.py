"""FastMCP entry point for the minimax-remaining-mcp server.

Run with stdio transport (compatible with DSH's @deepseek-ai/dsh-mcp-client):

    python -m minimax_remaining_mcp.server

Environment variables (all optional, with sensible defaults):
    MINIMAX_DATA_DIR                       storage directory (default: ./data)
    MINIMAX_PAUSE_THRESHOLD_REMAINING_PCT  default 30 (pause when <30% remaining in 5h fixed window)
    MINIMAX_WINDOW_SECONDS                 default 18000 (5h) — fallback only
    MINIMAX_HEADFUL_ON_LOGIN               default 1
    MINIMAX_CAMOUFOX_OS                    auto-detected (windows / macos / linux)
    MINIMAX_CAMOUFOX_LOCALE                default zh-CN
    MINIMAX_HTTP_TIMEOUT                   default 15
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .api import (
    fetch_coding_plan_remains,
    fetch_usage_console,
    parse_coding_plan_remains,
    parse_usage_console,
)
from .config import AppConfig, load_config
from .storage import CookieStore, SessionStore, UsageCache
from .window import WindowStatus, get_or_create_window, note_consumption, to_status


log = logging.getLogger("minimax_remaining_mcp.server")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _should_pause_by_remaining_pct(remaining_pct: int | None, threshold_pct: int) -> bool:
    """Pause when remaining percent in the 5h window is below threshold.

    Semantics aligned with the web UI ("5h 限额 30%"):
      - remaining_pct is what the page shows next to "限额"
      - threshold_pct = 30 means "pause when less than 30% remains"
        (i.e. when more than 70% of the 5h quota has been consumed).
    """
    if remaining_pct is None:
        return False
    return remaining_pct < threshold_pct


def _build_status_payload(
    cfg: AppConfig,
    *,
    usage: dict[str, Any] | None,
    window_status: WindowStatus,
    source: str,
    note: str | None = None,
    error: str | None = None,
    paused: bool = False,
) -> dict[str, Any]:
    """Compose the tool result.

    ``usage`` here is the *parsed* payload from whichever endpoint won
    (coding_plan_remains preferred). The fields are the canonical
    "5h window" view, plus supplementary plan info if we got it from
    the legacy console endpoint.
    """
    rem = (usage or {}).get("interval_remaining_percent") if usage else None
    return {
        "ok": error is None,
        "source": source,
        "fetched_at": _utc_now_iso(),
        # --- The numbers the user actually sees on the page: ----
        "primary_model": (usage or {}).get("primary_model") if usage else None,
        "remaining_percent_5h": rem,
        "used_percent_5h": (usage or {}).get("interval_used_percent") if usage else None,
        "seconds_until_reset": (usage or {}).get("seconds_until_reset") if usage else None,
        "seconds_until_reset_human": (usage or {}).get("seconds_until_reset_human") if usage else None,
        "interval_end_iso": (usage or {}).get("interval_end_iso") if usage else None,
        "interval_status_text": (usage or {}).get("interval_status_text") if usage else None,
        # Weekly window:
        "remaining_percent_weekly": (usage or {}).get("weekly_remaining_percent") if usage else None,
        "used_percent_weekly": (usage or {}).get("weekly_used_percent") if usage else None,
        "seconds_until_weekly_reset": (usage or {}).get("seconds_until_weekly_reset") if usage else None,
        "seconds_until_weekly_reset_human": (usage or {}).get("seconds_until_weekly_reset_human") if usage else None,
        "weekly_end_iso": (usage or {}).get("weekly_end_iso") if usage else None,
        # --- Supplementary plan info from the legacy endpoint: ----
        "total_credits": (usage or {}).get("total_credits") if usage else None,
        "used_credits": (usage or {}).get("used_credits") if usage else None,
        "remaining_credits": (usage or {}).get("remaining_credits") if usage else None,
        "user_name": (usage or {}).get("user_name") if usage else None,
        "group_id": (usage or {}).get("group_id") if usage else None,
        "package_name": (usage or {}).get("package_name") if usage else None,
        "package_expiration_iso": (usage or {}).get("package_expiration_iso") if usage else None,
        # --- Agent-local observation window (separate from MiniMax's): ---
        "window": {
            "is_active": window_status.is_active,
            "seconds_remaining": window_status.seconds_remaining,
            "seconds_remaining_human": window_status.seconds_remaining_human,
            "consumption_estimate": window_status.consumption_estimate,
            "start_iso": window_status.start_iso,
            "end_iso": window_status.end_iso,
        },
        "pause_threshold_remaining_pct": cfg.pause_threshold_remaining_pct,
        "should_pause": paused or _should_pause_by_remaining_pct(
            rem, cfg.pause_threshold_remaining_pct
        ),
        "warning": (usage or {}).get("warning") if usage else None,
        "status_code": (usage or {}).get("status_code") if usage else None,
        "model_remains": (usage or {}).get("model_remains") if usage else None,
        "note": note,
        "error": error,
        "version": __version__,
    }


# ----------------------------------------------------------------------
# FastMCP tool registrations
# ----------------------------------------------------------------------


def register_tools(mcp: Any, cfg: AppConfig) -> None:
    cookie_store = CookieStore(cfg.data_dir)
    session_store = SessionStore(cfg.data_dir)
    usage_cache = UsageCache(cfg.data_dir)

    @mcp.tool()
    async def minimax_status() -> dict[str, Any]:
        """Return current Token Plan status (the same numbers shown on the page).

        Reads the 5h fixed-window remaining/used percent, time until
        reset, and weekly window. Use this before any substantial work
        that will call MiniMax models. If ``should_pause`` is true, the
        agent should sleep until the 5h window resets (see
        minimax_wait_for_quota).

        The web console actually fires two distinct requests; we mirror
        both so the response matches what the user sees on screen:

        1. ``/v1/api/openplatform/coding_plan/remains?GroupId={gid}``
           powers the "5h 限额 / X% 已用 / 2h56m 后重置" panel.
        2. ``/backend/account/token_plan_credit`` powers the "套餐用量"
           panel (total / used / remaining credits).

        Auth for both uses the persisted web-session cookies
        (Camoufox capture). When cookies are missing or stale, the
        failure body is saved to ``<data_dir>/last_coding_plan_failure.json``
        so the cause can be diagnosed.
        """
        window_state = get_or_create_window(cfg, _WindowStoreShim(cfg))
        ws = to_status(window_state)

        # --- 1. Primary: Coding Plan remains (5h window percent) -----
        coding_plan_err: str | None = None
        usage: dict[str, Any] | None = None
        try:
            payload = await asyncio.to_thread(fetch_coding_plan_remains, cfg, cookie_store)
            usage = parse_coding_plan_remains(payload)
            usage_cache.save({"source": "coding_plan", "data": usage})
            _touch_session(session_store, "ok")
        except Exception as e:  # noqa: BLE001
            coding_plan_err = type(e).__name__ + ": " + str(e)[:300]
            log.warning("coding_plan path failed: %s. Will fill in package data from console_api.", e)

        # --- 2. Supplementary: console / token_plan_credit (package totals) ---
        # Always call this when the primary path failed, so we still have
        # package-level credits to show. Even when primary succeeds, we
        # merge package info so the agent sees one unified view.
        try:
            console_payload = await asyncio.to_thread(fetch_usage_console, cfg, cookie_store)
            console_usage = parse_usage_console(console_payload)
            if usage is None:
                # coding_plan didn't work — surface what we have.
                usage_cache.save({"source": "console_api", "data": console_usage})
                _touch_session(session_store, "ok")
                return _build_status_payload(
                    cfg,
                    usage=console_usage,
                    window_status=ws,
                    source="console_api",
                    note=(
                        "coding_plan/remains unavailable; showing package-level "
                        "totals only (no 5h window percent). "
                        + (coding_plan_err or "")
                    ).strip(),
                )
            # Merge package info into the coding_plan view.
            for k in (
                "total_credits", "used_credits", "remaining_credits",
                "user_name", "group_id", "package_name",
                "package_expiration_iso",
            ):
                if console_usage.get(k) is not None and (usage.get(k) is None or usage.get(k) == 0):
                    usage[k] = console_usage.get(k)
            _touch_session(session_store, "ok")
        except Exception as e:  # noqa: BLE001
            log.warning("Console path also failed: %s", e)
            if usage is None:
                # Both paths failed.
                err = coding_plan_err or (type(e).__name__ + ": " + str(e)[:200])
                return _build_status_payload(
                    cfg,
                    usage=None,
                    window_status=ws,
                    source="coding_plan",
                    error=err,
                    note=(
                        "Both coding_plan and console_api failed. Cookies likely "
                        "expired — call minimax_login() to refresh, then retry."
                    ),
                )
            # Primary path worked; console failed. Use primary as-is.
            log.info("Primary coding_plan path OK; console supplement failed (non-fatal).")

        return _build_status_payload(
            cfg, usage=usage, window_status=ws, source="coding_plan"
        )

    @mcp.tool()
    async def minimax_login(timeout_seconds: int = 600) -> dict[str, Any]:
        """Launch a headful Camoufox browser so the user can log in manually.

        Returns once the _token cookie appears in the browser. The
        server detects login success automatically; no further action
        needed.
        """
        from .browser import login_interactive
        # Override headful for this call (login is always headful).
        prev_headful = cfg.headful_on_login
        cfg.headful_on_login = True
        try:
            return await login_interactive(
                cfg, cookie_store, session_store
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:200]}
        finally:
            cfg.headful_on_login = prev_headful

    @mcp.tool()
    async def minimax_window() -> dict[str, Any]:
        """Return only the agent-local 5h observation window (no API call)."""
        window_state = get_or_create_window(cfg, _WindowStoreShim(cfg))
        ws = to_status(window_state)
        return {
            "is_active": ws.is_active,
            "seconds_remaining": ws.seconds_remaining,
            "seconds_remaining_human": ws.seconds_remaining_human,
            "consumption_estimate": ws.consumption_estimate,
            "start_iso": ws.start_iso,
            "end_iso": ws.end_iso,
            "note": (
                "MiniMax does not expose per-window request counts to clients. "
                "consumption_estimate reflects the agent self-report "
                "via minimax_consume(); treat it as advisory."
            ),
        }

    @mcp.tool()
    async def minimax_consume(delta: int = 1) -> dict[str, Any]:
        """Increment the local 5h-window consumption counter by ``delta``.

        Call this once per MiniMax API call (or batch) your agent makes.
        Combined with ``minimax_status()`` it lets the agent pace itself
        without waiting for MiniMax's 5h rate limit to bite.
        """
        if delta == 0:
            return {"ok": True, "delta": 0, "noop": True}
        state = note_consumption(cfg, _WindowStoreShim(cfg), delta)
        ws = to_status(state)
        return {
            "ok": True,
            "delta": delta,
            "consumption_estimate": state.consumption_estimate,
            "seconds_remaining_human": ws.seconds_remaining_human,
        }

    @mcp.tool()
    async def minimax_wait_for_quota(
        target_remaining_percent: int | None = None,
        poll_seconds: int = 60,
    ) -> dict[str, Any]:
        """Block until the 5h window remaining percent exceeds ``target_remaining_percent``.

        Polls ``minimax_status()`` every ``poll_seconds`` and exits when
        the threshold is satisfied (default = ``pause_threshold_remaining_pct``).
        Cancellable by closing the MCP connection. Intended for use by an
        agent that wants to truly pause (not just sleep a fixed amount).
        """
        threshold = (
            target_remaining_percent
            if target_remaining_percent is not None
            else cfg.pause_threshold_remaining_pct
        )
        while True:
            status = await minimax_status()
            if not status.get("should_pause"):
                return {
                    "ok": True,
                    "resumed_at": _utc_now_iso(),
                    "remaining_percent_5h": status.get("remaining_percent_5h"),
                    "seconds_until_reset_human": status.get("seconds_until_reset_human"),
                    "threshold_remaining_percent": threshold,
                    "poll_seconds": poll_seconds,
                }
            await asyncio.sleep(poll_seconds)

    @mcp.tool()
    async def minimax_clear(confirm: bool = False) -> dict[str, Any]:
        """Wipe persisted cookies, session metadata, and window state.

        Requires ``confirm=True`` to actually delete (otherwise returns
        a preview of what would be removed).
        """
        files = [
            cookie_store.path,
            session_store.path,
            usage_cache.path,
        ]
        win = _WindowStoreShim(cfg)
        files.append(win.path)
        existing = [str(p) for p in files if p.exists()]
        if not confirm:
            return {"ok": True, "would_remove": existing, "confirm_required": True}
        for p in files:
            try:
                if p.exists():
                    p.unlink()
            except OSError as e:
                log.warning("Could not remove %s: %s", p, e)
        return {"ok": True, "removed": existing}

    @mcp.tool()
    async def minimax_smoke() -> dict[str, Any]:
        """Quick health check: launch headless Camoufox and load example.com."""
        from .browser import smoke_test
        try:
            result = await smoke_test(cfg)
            return {"ok": bool(result.get("ok")), "details": result}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:200]}

    @mcp.tool()
    async def minimax_info() -> dict[str, Any]:
        """Return static configuration + last-known session metadata."""
        sess = session_store.load()
        return {
            "version": __version__,
            "config": {
                "data_dir": str(cfg.data_dir),
                "profile_dir": str(cfg.profile_dir),
                "usage_api_url": cfg.usage_api_url,
                "remains_api_url": cfg.remains_api_url,
                "remains_api_url_fallback": cfg.remains_api_url_fallback,
                "login_hint_url": cfg.login_hint_url,
                "pause_threshold_remaining_pct": cfg.pause_threshold_remaining_pct,
                "window_seconds": cfg.window_seconds,
                "headful_on_login": cfg.headful_on_login,
                "camoufox_os": cfg.camoufox_os,
                "camoufox_locale": cfg.camoufox_locale,
                "http_timeout_seconds": cfg.http_timeout_seconds,
            },
            "session": {
                "logged_in": sess.logged_in,
                "user_name": sess.user_name,
                "group_id": sess.group_id,
                "login_at": sess.login_at,
                "last_query_at": sess.last_query_at,
                "last_status": sess.last_status,
            },
        }


# Helper shim to avoid importing the WindowStore directly (keeps the
# tool implementations tidy).
class _WindowStoreShim:
    def __init__(self, cfg: AppConfig) -> None:
        from .storage import WindowStore
        self._real = WindowStore(cfg.data_dir)
        self.path = self._real.path
        self.load = self._real.load
        self.save = self._real.save
        self.clear = self._real.clear


def _touch_session(session_store: SessionStore, status: str) -> None:
    info = session_store.load()
    info.last_query_at = _utc_now_iso()
    info.last_status = status
    if info.logged_in and not info.user_name:
        # Already logged in, no name captured
        pass
    session_store.save(info)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # MCP stdio uses stdout for the protocol
    )

    try:
        from fastmcp import FastMCP
    except ImportError:
        log.error("fastmcp is not installed. Run `uv pip install fastmcp`.")
        sys.exit(1)

    cfg = load_config()
    mcp = FastMCP("minimax-token-plan")
    register_tools(mcp, cfg)

    log.info("Starting MiniMax MCP server v%s (data_dir=%s)", __version__, cfg.data_dir)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
