"""Persistent storage for cookies, session metadata, and window tracking.

All state lives as plain JSON files in the data directory so the agent can
inspect or back them up easily. Writes are atomic (write-temp-then-rename).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# File names
COOKIES_FILE = "cookies.json"
SESSION_FILE = "session.json"
WINDOW_FILE = "window.json"
LAST_USAGE_FILE = "last_usage.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically to avoid partial files if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ----------------------------------------------------------------------
# Cookie storage
# ----------------------------------------------------------------------


@dataclass
class Cookie:
    name: str
    value: str
    domain: str = ".minimaxi.com"
    path: str = "/"
    expires: Optional[float] = None  # seconds since epoch, None = session
    http_only: bool = False
    secure: bool = True
    same_site: str = "Lax"

    @classmethod
    def from_playwright(cls, c: dict[str, Any]) -> "Cookie":
        """Normalize a Playwright cookie dict to our Cookie shape."""
        exp = c.get("expires")
        # Playwright uses -1 for session cookies
        if isinstance(exp, (int, float)) and exp < 0:
            exp = None
        return cls(
            name=c["name"],
            value=c["value"],
            domain=c.get("domain", ".minimaxi.com") or ".minimaxi.com",
            path=c.get("path", "/") or "/",
            expires=float(exp) if exp is not None else None,
            http_only=bool(c.get("httpOnly", False)),
            secure=bool(c.get("secure", True)),
            same_site=c.get("sameSite", "Lax") or "Lax",
        )

    def to_requests_cookie(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
        }


class CookieStore:
    """Persist browser cookies across MCP server restarts."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / COOKIES_FILE

    def load(self) -> list[Cookie]:
        raw = _read_json(self.path)
        if not raw:
            return []
        out: list[Cookie] = []
        for entry in raw:
            try:
                out.append(Cookie(**entry))
            except TypeError:
                # tolerate older schema changes
                continue
        return out

    def save(self, cookies: list[Cookie]) -> None:
        _atomic_write_json(self.path, [asdict(c) for c in cookies])

    def save_from_playwright(self, pw_cookies: list[dict[str, Any]]) -> list[Cookie]:
        cookies = [Cookie.from_playwright(c) for c in pw_cookies]
        self.save(cookies)
        return cookies

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


# ----------------------------------------------------------------------
# Session metadata
# ----------------------------------------------------------------------


@dataclass
class SessionInfo:
    logged_in: bool = False
    user_name: Optional[str] = None
    group_id: Optional[str] = None
    login_at: Optional[str] = None
    last_query_at: Optional[str] = None
    last_status: Optional[str] = None  # "ok" | "expired" | "rate_limited" | "error"


class SessionStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / SESSION_FILE

    def load(self) -> SessionInfo:
        raw = _read_json(self.path)
        if not raw:
            return SessionInfo()
        try:
            return SessionInfo(**raw)
        except TypeError:
            return SessionInfo()

    def save(self, info: SessionInfo) -> None:
        _atomic_write_json(self.path, asdict(info))


# ----------------------------------------------------------------------
# Agent-local 5h window tracker (separate from MiniMax's fixed windows)
# ----------------------------------------------------------------------


@dataclass
class WindowState:
    """Tracks a single agent-local 5h observation window.

    MiniMax's actual 5h window is **fixed** (CST-aligned buckets per the
    Token Plan docs). This struct instead tracks the agent's *own*
    observation cycle so the agent can pace its MiniMax calls between
    status checks. consumption_estimate is set by the agent via
    ``minimax_consume()`` (advisory only).
    """

    window_start_iso: str
    window_end_iso: str
    consumption_estimate: int = 0  # requests used in this window (agent-supplied)
    last_noted_iso: Optional[str] = None

    @property
    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return datetime.fromisoformat(self.window_start_iso) <= now < datetime.fromisoformat(
            self.window_end_iso
        )

    @property
    def seconds_remaining(self) -> float:
        end = datetime.fromisoformat(self.window_end_iso)
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds())


class WindowStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / WINDOW_FILE

    def load(self) -> Optional[WindowState]:
        raw = _read_json(self.path)
        if not raw:
            return None
        try:
            return WindowState(**raw)
        except TypeError:
            return None

    def save(self, state: WindowState) -> None:
        _atomic_write_json(self.path, asdict(state))

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


# ----------------------------------------------------------------------
# Last observed usage cache (helps agent reason about trends)
# ----------------------------------------------------------------------


class UsageCache:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / LAST_USAGE_FILE

    def load(self) -> Optional[dict[str, Any]]:
        return _read_json(self.path)

    def save(self, payload: dict[str, Any]) -> None:
        payload["cached_at"] = _now_iso()
        _atomic_write_json(self.path, payload)
