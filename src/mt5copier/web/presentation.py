"""Pure view-model logic for the web dashboard: status tiers, row data, log tails.

Kept separate from ``routes.py`` so the mapping from engine state to a display tier, and the
log-tail arithmetic, can be unit tested without an ASGI app or a websocket -- the same reason
``sizing.py`` and ``symbols.py`` stay pure and separate from the I/O-heavy modules.

Status tiers follow the mapping decided in WEBUI_PLAN.md's "UI feel" section, reusing this
project's own existing vocabulary (``protocol.WorkerState``, ``TerminalView.status()``,
``Engine.holds()``, ``Engine.blocked_reason``) rather than inventing a parallel one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..engine import Engine, TerminalView

Tier = str
"""One of "good" / "warning" / "serious" / "critical" / "muted" -- see WEBUI_PLAN.md."""

_TIER_BY_STATUS: dict[str, Tier] = {
    "connected": "good",
    "connecting": "warning",
    "starting": "warning",
    "stopping": "warning",
    "disconnected": "serious",
    "stale": "serious",
    "worker dead": "critical",
    "failed": "critical",
    "not connected": "muted",
    "-": "muted",
    "stopped": "muted",
}


def tier_for_status(status_text: str) -> Tier:
    """A held slave overrides this to "warning" at the call site (see :func:`row_info`) --
    holding is orthogonal to connection status, so it is not folded into this table."""
    return _TIER_BY_STATUS.get(status_text, "muted")


@dataclass(frozen=True, slots=True)
class RowInfo:
    """Everything one dashboard row needs to render, already derived from a ``TerminalView``."""

    terminal_id: str
    label: str
    role: str
    status_text: str
    tier: Tier
    hold_reason: str | None
    login: int | None
    server: str | None
    balance: float | None
    equity: float | None
    floating_pl: float | None
    floating_pl_tier: Tier | None


def row_info(view: TerminalView, *, timeout_s: float, hold_reason: str | None) -> RowInfo:
    status_text = view.status(timeout_s)
    tier = tier_for_status(status_text)
    if hold_reason is not None and tier == "good":
        # A held slave is still connected -- the connection is fine, copying to it isn't.
        tier = "warning"
    snap = view.snapshot
    balance = snap.balance if snap else None
    equity = snap.equity if snap else None
    floating_pl = None if balance is None or equity is None else equity - balance
    # Reuses the status good/critical tokens directly for P&L, per WEBUI_PLAN.md's collision
    # rule, instead of a second green/red pair kept in sync by hand.
    pl_tier: Tier | None = (
        None if floating_pl is None else ("good" if floating_pl >= 0 else "critical")
    )
    return RowInfo(
        terminal_id=view.cfg.terminal_id,
        label=view.cfg.label,
        role=str(view.cfg.role),
        status_text=status_text,
        tier=tier,
        hold_reason=hold_reason,
        login=snap.login if snap else None,
        server=snap.server if snap else None,
        balance=balance,
        equity=equity,
        floating_pl=floating_pl,
        floating_pl_tier=pl_tier,
    )


def all_rows(engine: Engine) -> list[RowInfo]:
    """One :class:`RowInfo` per terminal, in the same order ``engine.views()`` already sorts."""
    timeout = engine.config.heartbeat_timeout_s
    holds = engine.holds()
    return [
        row_info(v, timeout_s=timeout, hold_reason=holds.get(v.cfg.terminal_id))
        for v in engine.views()
    ]


@dataclass(frozen=True, slots=True)
class LogTail:
    text: str
    offset: int
    """Byte offset to resume reading from on the next call."""


def read_log_tail(path: Path, *, max_lines: int = 200) -> LogTail:
    """The last ``max_lines`` of the log file, plus the offset to resume from.

    A missing file (nothing logged yet, or a fresh install) reads as empty, not an error --
    nothing has gone wrong, there is simply nothing to show yet.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return LogTail(text="", offset=0)
    lines = data.decode("utf-8", errors="replace").splitlines()
    return LogTail(text="\n".join(lines[-max_lines:]), offset=len(data))


def read_new_log_lines(path: Path, since_offset: int) -> LogTail:
    """Bytes appended since ``since_offset``.

    A file smaller than ``since_offset`` means the log rotated underneath us; restarting from 0
    rather than clamping avoids a giant burst of "new" text from the fresh file's own backlog.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return LogTail(text="", offset=since_offset)
    if size < since_offset:
        since_offset = 0
    if size == since_offset:
        return LogTail(text="", offset=since_offset)
    try:
        with path.open("rb") as f:
            f.seek(since_offset)
            data = f.read()
    except OSError:
        return LogTail(text="", offset=since_offset)
    return LogTail(text=data.decode("utf-8", errors="replace"), offset=size)
