"""Configuration loader for the MiniMax MCP server.

Reads environment variables and resolves the runtime configuration.
Defaults are tuned for the MiniMax Token Plan (CN endpoint).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _detect_camoufox_os() -> str:
    """Auto-detect the Camoufox OS fingerprint from the host platform."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# Default locations (overridable). The data dir defaults to a folder
# named ``data`` next to the package source so it works portably on
# Windows, macOS, and Linux. Override with the ``MINIMAX_DATA_DIR`` env
# var if you want state stored elsewhere.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = Path(
    os.environ.get("MINIMAX_DATA_DIR", str(_PACKAGE_ROOT / "data"))
)
DEFAULT_PROFILE_DIR = DEFAULT_DATA_DIR / "profile"

# MiniMax web + API endpoints (CN endpoints)
WEB_CONSOLE_URL = os.environ.get("MINIMAX_WEB_URL", "https://platform.minimaxi.com")
USAGE_API_URL = os.environ.get(
    "MINIMAX_USAGE_API_URL",
    "https://www.minimaxi.com/backend/account/token_plan_credit",
)
# The correct endpoint for the 5h fixed-window usage data the user sees
# on the Token Plan page. Requires `?GroupId={group_id}` query param. Auth
# uses the persistent web session cookies (NOT a Bearer key — the
# `api_key` field returned by /backend/account/token_plan_credit is an
# access key, not a Coding Plan subscription key).
REMAINS_API_URL = os.environ.get(
    "MINIMAX_REMAINS_API_URL",
    "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains",
)
# Fallback endpoint for the same data (different subdomain).
REMAINS_API_URL_FALLBACK = os.environ.get(
    "MINIMAX_REMAINS_API_URL_FALLBACK",
    "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains",
)

# Browser behavior
LOGIN_HINT_URL = os.environ.get(
    "MINIMAX_LOGIN_HINT_URL",
    "https://platform.minimaxi.com/user-center/basic-information/interface-key",
)


@dataclass
class AppConfig:
    data_dir: Path = DEFAULT_DATA_DIR
    profile_dir: Path = DEFAULT_PROFILE_DIR
    usage_api_url: str = USAGE_API_URL
    remains_api_url: str = REMAINS_API_URL
    remains_api_url_fallback: str = REMAINS_API_URL_FALLBACK
    login_hint_url: str = LOGIN_HINT_URL

    # If the **5h window remaining percent** drops below this, the agent
    # should pause. The web console shows "已用 N%" — we compare the
    # *remaining* percent, so threshold=30 means "pause when more than
    # 70% has been consumed".
    pause_threshold_remaining_pct: int = int(
        os.environ.get("MINIMAX_PAUSE_THRESHOLD_REMAINING_PCT", "30")
    )

    # 5h window length, in seconds. Used as a fallback for the agent's
    # own observation cycle (see window.py). MiniMax's actual 5h
    # window is **fixed** and CST-aligned; this value does NOT change
    # the schedule returned by the API.
    window_seconds: int = int(os.environ.get("MINIMAX_WINDOW_SECONDS", "18000"))  # 5h

    # Browser behavior flags
    headful_on_login: bool = os.environ.get("MINIMAX_HEADFUL_ON_LOGIN", "1") == "1"
    camoufox_os: str = os.environ.get("MINIMAX_CAMOUFOX_OS", _detect_camoufox_os())
    camoufox_locale: str = os.environ.get("MINIMAX_CAMOUFOX_LOCALE", "zh-CN")

    # HTTP timeouts
    http_timeout_seconds: float = float(os.environ.get("MINIMAX_HTTP_TIMEOUT", "15"))

    # Header overrides for API request (minimizes Cloudflare flags when reusing session)
    request_referer: str = WEB_CONSOLE_URL + "/"
    request_origin: str = WEB_CONSOLE_URL

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    cfg = AppConfig()
    cfg.ensure_dirs()
    return cfg
