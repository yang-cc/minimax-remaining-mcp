"""MiniMax API client (HTTP with persistent cookies).

Two paths are supported:

1. Web console cookies (persistent Camoufox session) ->
   /v1/api/openplatform/coding_plan/remains?GroupId={group_id}
   Returns the same ``model_remains[]`` payload that powers the
   "5h 限额 / X% 已用 / 2h56m 后重置" panel. Auth uses the persisted
   ``_token`` cookie plus ``x-group-id`` header (which must match the
   ``minimax_group_id_v2`` cookie value).

2. Web console cookies -> /backend/account/token_plan_credit
   Returns the package-level total/remaining credits (subscription pool).

Both return a normalized dict that downstream tools / the agent
can reason about without depending on the wire format.

Why no Bearer key path?
------------------------
The web console's "api_key" field looks like an ``sk-cp-...`` token
but is **not** a Coding Plan subscription key. Using it as a Bearer
on /v1/api/openplatform/coding_plan/remains returns ``base_resp = {2062,
"no active token plan"}`` — informative, not actionable. The only
working auth path today is the persisted web session cookie.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .config import AppConfig
from .storage import Cookie, CookieStore

log = logging.getLogger("minimax_remaining_mcp.api")


# Mimic a plausible Chrome 151 user-agent. Camoufox will produce a
# different UA when the browser path is used, but the API path only
# needs to look non-robotic.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def _attach_cookies(session: requests.Session, cookies: list[Cookie]) -> None:
    for c in cookies:
        # Use domain-agnostic set when possible so requests doesn't
        # worry about scope matching.
        session.cookies.set(c.name, c.value, domain=c.domain.lstrip("."), path=c.path)


def _common_headers(cfg: AppConfig, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    h = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": cfg.request_referer,
        "Origin": cfg.request_origin,
    }
    if extra:
        h.update(extra)
    return h


# ----------------------------------------------------------------------
# Path 1: token_plan_credit (package-level totals)
# ----------------------------------------------------------------------


def fetch_usage_console(cfg: AppConfig, cookie_store: CookieStore) -> dict[str, Any]:
    """GET the token-plan credit endpoint using the Camoufox-persisted session."""
    cookies = cookie_store.load()
    if not cookies:
        raise RuntimeError("No persisted cookies. Call minimax_login() first.")

    session = requests.Session()
    _attach_cookies(session, cookies)
    headers = _common_headers(cfg, {"x-group-id": ""})

    # The x-group-id is sent by the real browser. Pull it from cookies
    # if present (it's stored under minimax_group_id_v2).
    for c in cookies:
        if c.name == "minimax_group_id_v2" and c.value:
            headers["x-group-id"] = c.value
            break

    resp = session.get(
        cfg.usage_api_url,
        headers=headers,
        timeout=cfg.http_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()


def parse_usage_console(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the web console response.

    Two observed shapes:

    1. Old (rich) shape returned by the
       /backend/account/token_plan_credit endpoint:

       { total_credits, used_credits, remaining_credits,
         credit_packages_details: [ { ...models..., expiration_time (ms) } ],
         members_credit_spending: [ { user_name, ... } ] }

    2. New (minimal) shape returned as of 2026-08:

       { total_credits, used_credits, remaining_credits,    # all 0
         api_key: "sk-cp-...",                              # SENSITIVE
         balance_breakdown: { total_balance, buckets: [...] },
         token_plan_credit_fallback_enabled: bool,
         base_resp: { status_code, status_msg } }

    We extract from both and never expose the api_key to callers.
    """
    # Defense in depth: never let an api_key / sk-cp- prefix leak out.
    sensitive = {"api_key", "access_key", "secret_key"}
    for k in list(payload.keys()):
        v = payload.get(k)
        if isinstance(v, str) and v.startswith("sk-cp-"):
            sensitive.add(k)

    out: dict[str, Any] = {
        "source": "console_api",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_credits": payload.get("total_credits"),
        "used_credits": payload.get("used_credits"),
        "remaining_credits": payload.get("remaining_credits"),
        "user_name": None,
        "group_id": None,
        "package_name": None,
        "package_expiration_iso": None,
        "models": [],
        # New-shape data:
        "balance_total": None,
        "balance_buckets": [],
        "fallback_enabled": payload.get("token_plan_credit_fallback_enabled"),
        # Diagnostic flags:
        "schema_version": "unknown",
        "raw_keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
        "_redacted_keys": sorted(sensitive),
    }

    # --- New schema (2026-08+): balance_breakdown.buckets[] ----------
    bb = payload.get("balance_breakdown") or {}
    if isinstance(bb, dict):
        out["balance_total"] = bb.get("total_balance")
        out["balance_buckets"] = bb.get("buckets") or []
        # If buckets is empty AND fallback_enabled, the rich token-plan
        # data wasn't returned. Mark this so the agent / UI can warn.
        if (not out["balance_buckets"]) and out.get("fallback_enabled"):
            out["schema_version"] = "new_minimal_fallback"
            out["warning"] = (
                "API returned the new minimal schema with empty "
                "balance_breakdown.buckets and fallback_enabled=true. "
                "The Token Plan subscription may not be linked, or the "
                "server is on a partial rollout. Visit "
                "https://platform.minimaxi.com/user-center/basic-information/interface-key "
                "to confirm plan status."
            )

    # --- Old schema: credit_packages_details[] ---------------------
    pkgs = payload.get("credit_packages_details") or []
    if pkgs and isinstance(pkgs[0], dict):
        out["schema_version"] = "old_rich"
        p = pkgs[0]
        out["group_id"] = p.get("group_id")
        out["package_name"] = p.get("package_name")
        out["models"] = p.get("models") or []
        exp_ms = p.get("expiration_time")
        if isinstance(exp_ms, (int, float)) and exp_ms > 0:
            out["package_expiration_iso"] = (
                datetime.fromtimestamp(exp_ms / 1000.0, tz=timezone.utc)
                .isoformat(timespec="seconds")
            )

    # --- User name from either shape --------------------------------
    members = payload.get("members_credit_spending") or []
    if members and isinstance(members[0], dict):
        out["user_name"] = members[0].get("user_name") or members[0].get("nickname")

    # If total_credits came back as 0 with new schema, also surface the
    # underlying bucket counts so the agent has *something* to reason
    # about. (Buckets may include model-specific allowances.)
    if out["schema_version"] == "new_minimal_fallback" and out["balance_buckets"]:
        out["schema_version"] = "new_with_buckets"

    return out


# ----------------------------------------------------------------------
# Path 2: Coding Plan remains endpoint (the headline 5h window data)
# ----------------------------------------------------------------------
#
# This is the endpoint that powers the "5h 限额 / 2h56m 后重置 / 61% 已用"
# display on the MiniMax Token Plan web page.
#
# Endpoint: GET https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains
#           ?GroupId={group_id}
# Auth: persistent web session cookies (NOT a Bearer key — the api_key
#       returned by /backend/account/token_plan_credit is NOT a Coding
#       Plan subscription key; using it as Bearer yields 2062).
#
# Response shape (observed 2026-08):
#   {
#     "model_remains": [
#       {
#         "model_name": "general" | "video" | <model-id>,
#         "current_interval_remaining_percent": 30,
#         "current_interval_status": 1,        # 1=normal, 2=exhausted, 3=inactive
#         "current_interval_total_count": 0,    # always 0 in observed data
#         "current_interval_usage_count": 0,    # always 0 in observed data
#         "current_weekly_remaining_percent": 100,
#         "current_weekly_status": 3,
#         "current_weekly_total_count": 0,
#         "current_weekly_usage_count": 0,
#         "end_time": 1787641200000,            # ms epoch, 5h window end
#         "start_time": 1787623200000,          # ms epoch, 5h window start
#         "remains_time": 10131564,            # ms until reset (5h)
#         "weekly_end_time": 1788105600000,     # ms epoch, week window end
#         "weekly_start_time": 1787500800000,
#         "weekly_remains_time": 474531564,
#       },
#       ...
#     ],
#     "base_resp": {"status_code": 0, "status_msg": "success"}
#   }
# Field semantics:
#   - current_interval_remaining_percent = 100% - used%   (5h window)
#   - current_interval_status 1 = active, 2 = exhausted, 3 = inactive
#   - remains_time (ms) = time until 5h window reset
#   - end_time (ms) = absolute epoch of next 5h window reset


def fetch_coding_plan_remains(
    cfg: AppConfig,
    cookie_store: CookieStore,
    *,
    group_id: str | None = None,
) -> dict[str, Any]:
    """Query the Coding Plan remains endpoint using web session cookies.

    Headers are aligned to what Chrome 151 actually sends for the same
    request (sec-ch-ua, sec-fetch-*, priority, etc.) — minimaxi.com's
    edge rejects requests that look too "robotic". The user's confirmed
    curl capture (2026-08) uses these exact headers.

    Retries on the configured fallback endpoint if the primary fails.
    On failure, the full response body is saved to
    ``<data_dir>/last_coding_plan_failure.json`` for diagnosis, then
    RuntimeError is raised.

    Group ID resolution order:
      1. Explicit ``group_id`` argument (preferred — usually the one
         the caller just got from ``/backend/account/token_plan_credit``).
      2. The ``minimax_group_id_v2`` cookie, if present.
      3. Otherwise: raise so the caller can decide whether to fall
         back to console_api only.
    """
    cookies = cookie_store.load()
    if not cookies:
        raise RuntimeError("No persisted cookies. Call minimax_login() first.")

    if not group_id:
        gid_cookie = next(
            (c for c in cookies if c.name == "minimax_group_id_v2"), None
        )
        group_id = gid_cookie.value if gid_cookie else ""
    if not group_id:
        raise RuntimeError(
            "group_id not provided and minimax_group_id_v2 cookie is missing. "
            "The coding_plan/remains endpoint requires it. Either re-login in "
            "Camoufox (which should set the cookie), or call minimax_status() "
            "twice — the first call hits /backend/account/token_plan_credit "
            "(works with just the _token cookie) and persists group_id to "
            "session.json, the second call uses that."
        )

    # Headers aligned with the user's confirmed browser capture for the
    # coding_plan/remains panel (Chrome 151, Windows, zh-CN).
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json",
        "origin": "https://platform.minimaxi.com",
        "priority": "u=1, i",
        "referer": "https://platform.minimaxi.com/user-center/payment/coding-plan",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        ),
        "x-group-id": group_id,
    }
    cookie_jar = {c.name: c.value for c in cookies}

    endpoints = [cfg.remains_api_url]
    if cfg.remains_api_url_fallback and cfg.remains_api_url_fallback not in endpoints:
        endpoints.append(cfg.remains_api_url_fallback)

    last_err: Exception | None = None
    saw_auth_failure = False
    for url in endpoints:
        full_url = f"{url}?GroupId={group_id}"
        try:
            resp = requests.get(
                full_url,
                headers=headers,
                cookies=cookie_jar,
                timeout=cfg.http_timeout_seconds,
            )
            if resp.status_code == 200:
                body = resp.json()
                # Light-touch validation: must have base_resp.
                if not isinstance(body, dict) or "base_resp" not in body:
                    raise RuntimeError(
                        f"Unexpected payload (no base_resp) from {full_url}"
                    )
                body["_endpoint"] = url  # remember which endpoint answered
                return body
            # Non-200: capture full body for diagnosis.
            body_preview = (resp.text or "")[:2000]
            last_err = RuntimeError(
                f"HTTP {resp.status_code} from {full_url}; body[:2000]={body_preview!r}"
            )
            log.warning(
                "coding_plan endpoint %s returned %s; body=%s",
                full_url, resp.status_code, body_preview,
            )
            if resp.status_code in (401, 403):
                saw_auth_failure = True
            # Persist the last failure so the user/agent can inspect it.
            try:
                diag_path = cfg.data_dir / "last_coding_plan_failure.json"
                diag_path.write_text(
                    json.dumps(
                        {
                            "url": full_url,
                            "status_code": resp.status_code,
                            "headers_sent": dict(headers),
                            "body": resp.text,
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError as e:
                log.warning("could not persist failure dump: %s", e)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("coding_plan endpoint %s failed: %s", full_url, e)

    if saw_auth_failure:
        raise RuntimeError(
            "coding_plan/remains returned 401/403 — web session cookies have expired. "
            "Call minimax_login() to refresh the Camoufox session, then retry."
        )
    raise RuntimeError(
        f"All coding_plan endpoints failed; last error: {last_err!r}"
    )


def parse_coding_plan_remains(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the Coding Plan remains response.

    Selects the entry that has the smallest remaining percent (the most
    constrained window — typically `general` for text-based APIs), and
    also returns the full list for transparency.
    """
    out: dict[str, Any] = {
        "source": "coding_plan",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status_code": None,
        "status_msg": None,
        "model_remains": [],
        # Headline fields (from the most-constrained entry):
        "primary_model": None,
        "interval_remaining_percent": None,
        "interval_used_percent": None,
        "interval_status": None,
        "interval_status_text": None,
        "interval_start_iso": None,
        "interval_end_iso": None,
        "seconds_until_reset": None,
        "seconds_until_reset_human": None,
        # Weekly window:
        "weekly_remaining_percent": None,
        "weekly_used_percent": None,
        "weekly_status": None,
        "weekly_end_iso": None,
        "seconds_until_weekly_reset": None,
        "seconds_until_weekly_reset_human": None,
        # Endpoint source for debugging.
        "_endpoint": payload.get("_endpoint"),
    }

    base = payload.get("base_resp") or {}
    if isinstance(base, dict):
        out["status_code"] = base.get("status_code")
        out["status_msg"] = base.get("status_msg")

    mr = payload.get("model_remains")
    if not isinstance(mr, list):
        out["warning"] = (
            "coding_plan endpoint returned no model_remains array. "
            f"base_resp.status_msg={out['status_msg']!r}"
        )
        return out

    # Build per-model summaries.
    summaries: list[dict[str, Any]] = []
    for entry in mr:
        if not isinstance(entry, dict):
            continue
        ipct = entry.get("current_interval_remaining_percent")
        wpct = entry.get("current_weekly_remaining_percent")
        end_ms = entry.get("end_time")
        wend_ms = entry.get("weekly_end_time")
        rt_ms = entry.get("remains_time")
        wrt_ms = entry.get("weekly_remains_time")
        st_ms = entry.get("start_time")
        wst_ms = entry.get("weekly_start_time")
        summaries.append(
            {
                "model_name": entry.get("model_name"),
                "interval_remaining_percent": ipct,
                "interval_used_percent": (100 - ipct) if isinstance(ipct, (int, float)) else None,
                "interval_status": entry.get("current_interval_status"),
                "interval_start_iso": (
                    datetime.fromtimestamp(st_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                    if isinstance(st_ms, (int, float)) and st_ms > 0 else None
                ),
                "interval_end_iso": (
                    datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                    if isinstance(end_ms, (int, float)) and end_ms > 0 else None
                ),
                "seconds_until_reset": (rt_ms / 1000) if isinstance(rt_ms, (int, float)) and rt_ms > 0 else None,
                "weekly_remaining_percent": wpct,
                "weekly_used_percent": (100 - wpct) if isinstance(wpct, (int, float)) else None,
                "weekly_status": entry.get("current_weekly_status"),
                "weekly_end_iso": (
                    datetime.fromtimestamp(wend_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                    if isinstance(wend_ms, (int, float)) and wend_ms > 0 else None
                ),
                "seconds_until_weekly_reset": (wrt_ms / 1000) if isinstance(wrt_ms, (int, float)) and wrt_ms > 0 else None,
            }
        )
    out["model_remains"] = summaries

    # Pick the most-constrained entry: smallest remaining percent, then
    # prefer entries with status=1 (active). If none are active, pick the
    # first non-null entry.
    candidates = [s for s in summaries if s["interval_remaining_percent"] is not None]
    if candidates:
        active = [s for s in candidates if s["interval_status"] == 1]
        pool = active if active else candidates
        head = min(pool, key=lambda s: s["interval_remaining_percent"])
        out["primary_model"] = head["model_name"]
        out["interval_remaining_percent"] = head["interval_remaining_percent"]
        out["interval_used_percent"] = head["interval_used_percent"]
        out["interval_status"] = head["interval_status"]
        out["interval_status_text"] = (
            "active" if head["interval_status"] == 1
            else "exhausted" if head["interval_status"] == 2
            else "inactive" if head["interval_status"] == 3
            else f"status_{head['interval_status']}"
        )
        out["interval_start_iso"] = head["interval_start_iso"]
        out["interval_end_iso"] = head["interval_end_iso"]
        out["seconds_until_reset"] = head["seconds_until_reset"]
        if head["seconds_until_reset"] is not None:
            sec = head["seconds_until_reset"]
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            out["seconds_until_reset_human"] = f"{h}h{m:02d}m{s:02d}s"
        out["weekly_remaining_percent"] = head["weekly_remaining_percent"]
        out["weekly_used_percent"] = head["weekly_used_percent"]
        out["weekly_status"] = head["weekly_status"]
        out["weekly_end_iso"] = head["weekly_end_iso"]
        out["seconds_until_weekly_reset"] = head["seconds_until_weekly_reset"]
        if head["seconds_until_weekly_reset"] is not None:
            sec = head["seconds_until_weekly_reset"]
            d = int(sec // 86400)
            h = int((sec % 86400) // 3600)
            m = int((sec % 3600) // 60)
            out["seconds_until_weekly_reset_human"] = (
                f"{d}d{h:02d}h{m:02d}m" if d > 0 else f"{h}h{m:02d}m"
            )

    return out