"""Camoufox browser wrapper for login + cookie persistence.

Camoufox (https://github.com/daijro/camoufox) is an anti-detect Firefox
build with engine-level fingerprint spoofing. We use its persistent
profile so that cookies survive between MCP server restarts; the
browser is only launched when we need a fresh login.

Why a browser at all?
- Cloudflare's anti-bot flags suspicious API calls even when they reuse
  valid cookies, *unless* the same TLS/HTTP fingerprint is presented.
- Camoufox produces a Firefox TLS fingerprint that differs from the
  cf-attracting Chrome signatures, plus a humanized mouse / cursor
  pattern that keeps the session believable across navigations.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig
from .storage import CookieStore, SessionStore

log = logging.getLogger("minimax_remaining_mcp.browser")


class CamoufoxLoginError(RuntimeError):
    pass


async def _new_page_with_persistent_profile(cfg: AppConfig) -> tuple[Any, Any]:
    """Open Camoufox with a persistent Firefox profile and return (browser, page).

    Caller is responsible for `await cf.__aexit__(...)` to close cleanly.
    """
    from camoufox.async_api import AsyncCamoufox

    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    headless = not cfg.headful_on_login
    cf = AsyncCamoufox(
        headless=headless,
        humanize=True,
        locale=cfg.camoufox_locale,
        os=cfg.camoufox_os,
        persistent_context=True,
        user_data_dir=str(cfg.profile_dir),
        geoip=True,
    )
    browser = await cf.__aenter__()
    page = await browser.new_page()
    return cf, page  # cf is the context manager wrapper; call its __aexit__


async def collect_cookies_from_page(page: Any) -> list[dict[str, Any]]:
    """Snapshot cookies from the page's underlying context."""
    ctx = page.context
    return await ctx.cookies()


async def login_interactive(
    cfg: AppConfig,
    cookie_store: CookieStore,
    session_store: SessionStore,
) -> dict[str, Any]:
    """Launch a (headful) Camoufox, wait for the user to log in, then save cookies.

    The agent calls this when cookies are missing or expired. The user
    must solve any Cloudflare / CAPTCHA challenge manually because we do
    not integrate with any solver service (out of scope).
    """
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)

    # Login always uses a visible window. We override only that one flag
    # via dataclasses.replace; everything else inherits from ``cfg``.
    cfg_local = replace(cfg, headful_on_login=True)

    cf, page = await _new_page_with_persistent_profile(cfg_local)
    try:
        await page.goto(
            cfg_local.login_hint_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        hint_lines = [
            "===========================================================",
            "MiniMax MCP - Login Required",
            "===========================================================",
            "",
            f"A Camoufox browser is now open at: {cfg_local.login_hint_url}",
            "",
            "Please:",
            "  1. Solve any Cloudflare / CAPTCHA challenge",
            "  2. Log into MiniMax normally (phone / email / OAuth)",
            "  3. Wait until you reach the API Keys page",
            "",
            "When the page is logged-in, the MCP server will detect it",
            "automatically and persist your cookies.",
            "",
            "===========================================================",
        ]
        for line in hint_lines:
            log.info(line)

        # Poll for the session JWT cookie.
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        try:
            await _wait_for_login(page, cookie_store, session_store, timeout=600)
        except PlaywrightTimeoutError:
            raise CamoufoxLoginError(
                "Timed out waiting for the _token cookie to appear. "
                "Did you complete login in the browser window?"
            )

        info = session_store.load()
        info.logged_in = True
        info.login_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        session_store.save(info)
    finally:
        await cf.__aexit__(None, None, None)

    cookies = cookie_store.load()
    info = session_store.load()
    return {
        "ok": True,
        "saved_cookie_count": len(cookies),
        "user_name": info.user_name,
        "group_id": info.group_id,
        "login_at": info.login_at,
    }


async def _wait_for_login(
    page: Any,
    cookie_store: CookieStore,
    session_store: SessionStore,
    *,
    timeout: int = 600,
    poll_interval: float = 2.0,
) -> None:
    """Wait until the _token cookie is set, polling the page context."""
    import time
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    deadline = time.monotonic() + timeout
    ctx = page.context
    while time.monotonic() < deadline:
        cookies = await ctx.cookies()
        has_token = any(
            c.get("name") == "_token" and c.get("value")
            for c in cookies
        )
        if has_token:
            cookie_store.save_from_playwright(cookies)
            info = session_store.load()
            for c in cookies:
                if c.get("name") == "minimax_group_id_v2":
                    info.group_id = c.get("value")
            session_store.save(info)
            log.info("Login detected via _token cookie. Persisted %d cookies.", len(cookies))
            return
        await asyncio.sleep(poll_interval)
    raise PlaywrightTimeoutError(f"No login detected within {timeout}s")


async def smoke_test(cfg: AppConfig) -> dict[str, Any]:
    """Launch Camoufox headlessly, load example.com, return title. No persistence."""
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=True, humanize=True) as browser:
        page = await browser.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded", timeout=20000)
        title = await page.title()
        return {"ok": True, "title": title}
