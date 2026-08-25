"""Agent-local 5h window state machine.

The MiniMax Token Plan is governed by a **fixed** 5h window aligned
to Beijing time — typical boundaries are 10:00 / 15:00 / 20:00 CST etc.
(see https://platform.minimaxi.com/subscribe/token-plan). MiniMax does
not expose per-window consumption to clients, so this module tracks the
**agent's own observation window** for self-pacing awareness rather
than as an authoritative counter.

The agent window starts when the MCP server first runs and rolls over
after ``MINIMAX_WINDOW_SECONDS`` (default 5h). It is independent from
MiniMax's fixed windows, and is only used to compute a local
``consumption_estimate`` for display.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import AppConfig
from .storage import WindowState, WindowStore


@dataclass
class WindowStatus:
    is_active: bool
    seconds_remaining: float
    seconds_remaining_human: str
    consumption_estimate: int
    start_iso: str
    end_iso: str


def _humanize_seconds(s: float) -> str:
    if s <= 0:
        return "0s"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    if h > 0:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m > 0:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def get_or_create_window(cfg: AppConfig, store: WindowStore) -> WindowState:
    """Return the active window, creating one if none exists or the prior one expired."""
    now = datetime.now(timezone.utc)
    cur = store.load()
    if cur and datetime.fromisoformat(cur.window_end_iso) > now:
        return cur
    start = now
    end = now + timedelta(seconds=cfg.window_seconds)
    new_state = WindowState(
        window_start_iso=start.isoformat(timespec="seconds"),
        window_end_iso=end.isoformat(timespec="seconds"),
        consumption_estimate=0,
        last_noted_iso=now.isoformat(timespec="seconds"),
    )
    store.save(new_state)
    return new_state


def note_consumption(cfg: AppConfig, store: WindowStore, delta: int) -> WindowState:
    """Increment consumption_estimate on the active window (agent-supplied)."""
    state = get_or_create_window(cfg, store)
    state.consumption_estimate = max(0, state.consumption_estimate + delta)
    state.last_noted_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.save(state)
    return state


def to_status(state: WindowState) -> WindowStatus:
    remaining = state.seconds_remaining
    return WindowStatus(
        is_active=remaining > 0,
        seconds_remaining=remaining,
        seconds_remaining_human=_humanize_seconds(remaining),
        consumption_estimate=state.consumption_estimate,
        start_iso=state.window_start_iso,
        end_iso=state.window_end_iso,
    )
