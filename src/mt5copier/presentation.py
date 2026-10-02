"""Pure view-model logic for the TUI: status tiers, row data, money formatting, log tails.

Kept separate from ``tui/app.py`` so the mapping from engine state to a display tier, and the
log-tail arithmetic, can be unit tested without running Textual -- the same reason ``sizing.py``
and ``symbols.py`` stay pure and separate from the I/O-heavy modules.

Status tiers reuse this project's own existing vocabulary (``protocol.WorkerState``,
``TerminalView.status()``, ``Engine.holds()``, ``Engine.blocked_reason``) rather than inventing a
parallel one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Role
from .engine import Engine, TerminalView
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

STATUS_BADGE: dict[Tier, str] = {
    "good": "[OK]",
    "warning": "[WARN]",
    "serious": "[ERR]",
    "critical": "[DEAD]",
    "muted": "[OFF]",
}
"""Bracketed text badges. A status color never appears without a non-color cue, so the tier
still reads on a monochrome terminal or to someone who cannot tell the colors apart."""


def tier_for_status(status_text: str) -> Tier:
    """A held slave overrides this to "warning" at the call site (see :func:`row_info`) --
    holding is orthogonal to connection status, so it is not folded into this table."""
    return _TIER_BY_STATUS.get(status_text, "muted")


def badge_for_tier(tier: Tier) -> str:
    return STATUS_BADGE.get(tier, "[??]")


@dataclass(frozen=True, slots=True)
class RowInfo:
    """Everything one table row needs to render, already derived from a ``TerminalView``."""

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
    return ("Dry run" if dry_run else "Live"), ("warning" if dry_run else "good")


# ---------- money formatting ----------

_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "AUD": "$",
    "CAD": "$",
    "NZD": "$",
    "CHF": "Fr",
    "HKD": "$",
    "SGD": "$",
    "ZAR": "R",
}
"""Symbols for the currencies this project's brokers actually report. Not exhaustive on
purpose -- an ISO code this table doesn't know falls back to showing itself (see
:func:`format_money`) rather than guessing a symbol, or worse, a wrong one."""


def format_money(amount: float | None, currency: str | None, *, force_sign: bool = False) -> str:
    """Thousands-separated, two decimals, with a currency mark when the account's currency is
    known -- and **never** a fabricated one when it isn't. An unlabelled "$" on an account that
    might just as easily be EUR or GBP would be a wrong answer dressed as a right-looking one;
    showing the bare number, or the ISO code if that much is known, is honest instead.

    ``force_sign`` always shows a leading "+"/"-" even on a non-negative amount -- what floating
    P/L needs (so a gain never reads as "just a number") and balance/equity don't.
    """
    if amount is None:
        return "—"
    text = f"{'+' if force_sign and amount >= 0 else ''}{amount:,.2f}"
    if not currency:
        return text
    symbol = _CURRENCY_SYMBOLS.get(currency.upper())
    if symbol is None:
        return f"{text} {currency.upper()}"
    sign, digits = (text[0], text[1:]) if text[:1] in "+-" else ("", text)
    return f"{sign}{symbol}{digits}"


def all_rows(engine: Engine) -> list[RowInfo]:
    """One :class:`RowInfo` per terminal, in the same order ``engine.views()`` already sorts."""
    timeout = engine.config.heartbeat_timeout_s
    holds = engine.holds()
    return [
        row_info(
            v,
            timeout_s=timeout,
            hold_reason=holds.get(v.cfg.terminal_id),
            display_name=engine.config.display_name(v.cfg.terminal_id),
        )
        for v in engine.views()
    ]


@dataclass(frozen=True, slots=True)
class GroupedRows:
    """The same rows ``all_rows`` returns, partitioned by role for the terminal table: the
    master (if any) with its slaves, then everything unassigned.

    Partitioning, not a tree: ``config.py``'s one-master rule plus ``role_actions.py``'s
    no-master-no-slave rule mean there is at most one master and every slave belongs to it, so a
    flat "master" + "slaves" + "unassigned" grouping already says everything a deeper tree would
    -- and it stays this simple on purpose until multi-master actually exists (see ROADMAP.md's
    "Future features" section).
    """

    master: RowInfo | None
    slaves: list[RowInfo]
    unassigned: list[RowInfo]


def group_rows(rows: list[RowInfo]) -> GroupedRows:
    return GroupedRows(
        master=next((r for r in rows if r.role == "master"), None),
        slaves=[r for r in rows if r.role == "slave"],
        unassigned=[r for r in rows if r.role == "unassigned"],
    )


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


_LOG_LEVEL_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (\S+)")
_LOG_LEVEL_TIER: dict[str, Tier] = {
    "DEBUG": "muted",
    "INFO": "good",
    "WARNING": "warning",
    "ERROR": "serious",
    "CRITICAL": "critical",
}


def log_line_tier(line: str) -> Tier | None:
    """The tier to color one log line by, from its level -- otherwise an INFO heartbeat and a
    WARNING worth noticing are the same gray text. None for a line with no recognised level (a
    traceback continuation, for instance), which is shown plain rather than guessed at.
    """
    match = _LOG_LEVEL_RE.match(line)
    return _LOG_LEVEL_TIER.get(match.group(1)) if match else None
