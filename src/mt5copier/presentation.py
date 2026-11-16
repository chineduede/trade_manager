"""Pure view-model logic for the UI: status tiers, per-terminal display data, log tails.

Kept separate from the desktop bridge so the mapping from engine state to a display tier, and
the log-tail arithmetic, can be unit tested without a window -- the same reason ``sizing.py``
and ``symbols.py`` stay pure and separate from the I/O-heavy modules.

Status tiers reuse this project's own existing vocabulary (``protocol.WorkerState``,
``TerminalView.status()``, ``Engine.holds()``, ``Engine.blocked_reason``) rather than inventing a
parallel one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Role
from .engine import TerminalView
from .protocol import ACCOUNT_TRADE_MODE_DEMO

Tier = str
"""One of "good" / "warning" / "serious" / "critical" / "muted"."""

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
    """Everything a terminal's card needs to render, already derived from a ``TerminalView``."""

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
    currency: str | None
    open_trades: int | None
    """Count of open positions on this terminal right now, or None if unknown (no snapshot
    yet, or the last read failed -- see ``Snapshot.positions``'s own docstring)."""
    paused: bool
    """A slave the person has paused: connected and managing its copies, taking no new ones."""

    demo_guard_blocked: bool
    """True when this slave is connected but the live-only demo guard would currently refuse
    to trade it (``allow_live_trading`` is off and the account is not a readable demo) -- see
    ``Executor._guard_live_account``, whose exact policy this mirrors for display: an unreadable
    trade mode counts as *not* demo. Always False for a master or an unassigned terminal, and
    False while disconnected -- a link that is down is reported as that, not as this, since
    nothing would be sent to it right now regardless of the account type."""


def row_info(
    view: TerminalView, *, timeout_s: float, hold_reason: str | None, display_name: str
) -> RowInfo:
    status_text = view.status(timeout_s)
    tier = tier_for_status(status_text)
    if hold_reason is not None and tier == "good":
        # A held slave is still connected -- the connection is fine, copying to it isn't.
        tier = "warning"
    snap = view.snapshot
    balance = snap.balance if snap else None
    equity = snap.equity if snap else None
    floating_pl = None if balance is None or equity is None else equity - balance
    # Reuses the status good/critical tiers directly for P&L instead of a second green/red pair
    # kept in sync by hand.
    pl_tier: Tier | None = (
        None if floating_pl is None else ("good" if floating_pl >= 0 else "critical")
    )
    # Prefer the live account's own login/server; fall back to what discovery last read off
    # disk, so the account id is still shown before anything is connected.
    login = snap.login if snap and snap.login else view.cfg.last_login
    server = snap.server if snap and snap.server else view.cfg.last_server
    demo_guard_blocked = (
        view.cfg.role is Role.SLAVE
        and tier == "good"
        and not view.cfg.allow_live_trading
        and snap is not None
        and snap.trade_mode != ACCOUNT_TRADE_MODE_DEMO
    )
    return RowInfo(
        terminal_id=view.cfg.terminal_id,
        label=display_name,
        role=str(view.cfg.role),
        status_text=status_text,
        tier=tier,
        hold_reason=hold_reason,
        login=login,
        server=server,
        balance=balance,
        equity=equity,
        floating_pl=floating_pl,
        floating_pl_tier=pl_tier,
        currency=snap.currency if snap else None,
        open_trades=snap.positions if snap else None,
        paused=view.cfg.role is Role.SLAVE and view.cfg.paused,
        demo_guard_blocked=demo_guard_blocked,
    )


def slave_badge(row: RowInfo, dry_run: bool) -> tuple[str, Tier]:
    """The one-line state of a slave, picked in priority order -- a line only has room to say
    one thing, so this is the order in which facts stop being worth showing the
    rest of once one of them is true:

    1. **Connectivity.** Nothing below is still meaningful if the link itself is down.
    2. **The demo guard.** A live trading-safety refusal outranks the ambient dry-run label.
    3. **A hold** (unacknowledged orphans, an unreadable balance -- see ``Engine.holds()``).
    4. Otherwise, the ambient **Dry run / Live** label -- purely descriptive once nothing above
       applies.
    """
    if row.tier == "serious":
        return "Broker link lost", "serious"
    if row.tier == "critical":
        return "Terminal not responding", "critical"
    if row.tier == "muted":
        return "Not connected", "muted"
    if row.tier == "warning" and row.hold_reason is None:
        # Transient (connecting/starting/stopping) rather than a settled bad state.
        return row.status_text.capitalize(), "warning"
    if row.demo_guard_blocked:
        return "Held back: not a demo account", "critical"
    if row.hold_reason is not None:
        return row.hold_reason, "warning"
    if row.paused:
        return "Paused: no new trades are copied here", "warning"
    return ("Dry run" if dry_run else "Live"), ("warning" if dry_run else "good")


def read_log_tail(path: Path, *, max_lines: int = 200) -> str:
    """The last ``max_lines`` of the log file, as plain text.

    A missing file (nothing logged yet, or a fresh install) reads as empty, not an error --
    nothing has gone wrong, there is simply nothing to show yet.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])
