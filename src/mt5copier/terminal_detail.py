"""Pure view-model for the per-terminal detail page.

Everything the page shows is derived here from plain engine data -- a snapshot, the copies the
store believes in, the orphans, the copy-health stats -- so what a row says can be tested
without a window. The desktop detail page only draws it.

Display only, end to end: nothing here feeds back into what is copied.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .config import BreakevenMode, TerminalConfig
from .presentation import Tier
from .protocol import COPIER_MAGIC, Position, PositionLive, PositionType, Snapshot
from .reconcile import OpenCopy

MARGIN_LEVEL_DANGER = 100.0
"""Below this, the broker can start closing positions (most stop out at 50%, some at 100%)."""
MARGIN_LEVEL_WARN = 200.0


@dataclass(frozen=True, slots=True)
class PositionRow:
    """One open position, with the live values beside it and how it relates to the copier."""

    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    price_current: float | None
    sl: float
    tp: float
    sl_room: float | None
    """Distance from the current price to the stop, positive while the stop is not hit. None
    when there is no stop or no current price."""
    tp_room: float | None
    swap: float | None
    profit: float | None
    link: str
    """How this position relates to the copier, in a few words: whose copy it is, how many
    slaves hold it, or that it is an orphan or not ours at all."""
    link_tier: Tier
    digits: int
    """Decimals to show this symbol's prices with (see :func:`price_digits`)."""
    master_ticket: int | None = None
    """The master position this is a copy of, on a slave; None for anything that is not a
    tracked copy. What the breakeven keys act on."""
    breakeven: str = ""
    """This copy's breakeven state in a few words; blank for anything that is not a copy."""
    breakeven_tier: Tier = "muted"


@dataclass(frozen=True, slots=True)
class MarginGauge:
    used_fraction: float | None
    """Margin in use as a fraction of equity, 0..1 (clamped). None when unknown."""
    level_pct: float | None
    """Equity / margin in percent, or None when nothing is using margin or it is unknown."""
    level_tier: Tier
    free: float | None


@dataclass(frozen=True, slots=True)
class Exposure:
    symbol: str
    net_lots: float
    """Buys minus sells. Positive is net long."""
    gross_lots: float


def price_digits(*prices: float) -> int:
    """Decimals to print a symbol's prices with, read off the prices themselves.

    The snapshot does not carry the symbol's ``digits``, and asking the terminal on every tick
    to label a column is not worth it. Prices arrive already rounded to the symbol's precision,
    so the most decimals any of them needs is a good stand-in -- at worst a trailing zero short
    (1.0850 shown as 1.085), never a digit invented.
    """
    best = 0
    for p in prices:
        if not p:
            continue
        text = f"{p:.8f}".rstrip("0")
        decimals = len(text.split(".", 1)[1]) if "." in text else 0
        best = max(best, decimals)
    return min(best, 8)


def format_price(value: float | None, digits: int) -> str:
    if value is None or value == 0:
        return "—"
    return f"{value:,.{digits}f}"


def _room(ptype: PositionType, current: float | None, level: float, *, stop: bool) -> float | None:
    if not level or current is None:
        return None
    if ptype is PositionType.BUY:
        return current - level if stop else level - current
    return level - current if stop else current - level


def breakeven_rule(cfg: TerminalConfig) -> str:
    """A slave's own breakeven trigger, as the column shows it for a copy with nothing set by
    hand."""
    if cfg.breakeven_mode is BreakevenMode.R:
        return f"{cfg.breakeven_trigger_r:g}R"
    return "off"


def _breakeven_cell(copy: OpenCopy, rule: str, digits: int) -> tuple[str, Tier]:
    if copy.breakeven_exempt:
        return "exempt", "muted"
    # Its own decimals count too: a level is often one digit finer than the row's prices.
    if copy.breakeven_sl:
        return (
            f"✓ {format_price(copy.breakeven_sl, max(digits, price_digits(copy.breakeven_sl)))}",
            "good",
        )
    if copy.breakeven_at:
        return (
            f"at {format_price(copy.breakeven_at, max(digits, price_digits(copy.breakeven_at)))}",
            "warning",
        )
    return rule, "muted"


def position_rows(
    snap: Snapshot,
    *,
    is_master: bool,
    copies: Sequence[OpenCopy] | None = None,
    breakeven: str = "off",
    master_positions: Iterable[Position] | None = None,
    held_by: Mapping[int, int] | None = None,
    slave_count: int = 0,
    orphan_tickets: Iterable[int] = (),
) -> list[PositionRow] | None:
    """The positions table, or None when the positions could not be read.

    On a **slave**, ``copies`` are what the store believes this slave holds, and each copy is
    named after the master position it follows. On the **master**, ``held_by`` counts, per
    master ticket, how many slaves hold a copy, out of ``slave_count``. ``breakeven`` is the
    slave's own trigger (see :func:`breakeven_rule`), shown for a copy with nothing set by hand.
    """
    if snap.open_positions is None:
        return None
    live: dict[int, PositionLive] = {lp.ticket: lp for lp in snap.live_positions or ()}
    by_slave_ticket = {
        c.slave_ticket: c for c in copies or () if c.slave_ticket is not None and not c.resting
    }
    masters = {p.ticket: p for p in master_positions or ()}
    orphans = set(orphan_tickets)

    rows: list[PositionRow] = []
    for p in sorted(snap.open_positions, key=lambda p: (p.symbol, p.ticket)):
        lp = live.get(p.ticket)
        current = lp.price_current if lp else None
        digits = price_digits(p.price_open, p.sl, p.tp, current or 0.0)
        copy: OpenCopy | None = None
        if is_master:
            n = (held_by or {}).get(p.ticket, 0)
            if slave_count == 0:
                link, tier = "no slaves", "muted"
            else:
                link = f"on {n}/{slave_count} slaves"
                tier = "good" if n == slave_count else ("warning" if n else "serious")
        else:
            copy = by_slave_ticket.get(p.ticket)
            if copy is not None:
                master = masters.get(copy.master_ticket)
                if master is None:
                    link, tier = f"copy of #{copy.master_ticket} (closed on master)", "serious"
                else:
                    link, tier = (
                        f"copy of #{copy.master_ticket} (master {master.volume:g} lots)",
                        "good",
                    )
            elif p.ticket in orphans:
                link, tier = "ORPHAN: ours, untracked", "critical"
            elif p.magic == COPIER_MAGIC:
                link, tier = "ours, not yet matched", "warning"
            else:
                link, tier = "manual / not ours", "muted"
        be_text, be_tier = "", "muted"
        if copy is not None:
            be_text, be_tier = _breakeven_cell(copy, breakeven, digits)
        rows.append(
            PositionRow(
                ticket=p.ticket,
                symbol=p.symbol,
                side="BUY" if p.type is PositionType.BUY else "SELL",
                volume=p.volume,
                price_open=p.price_open,
                price_current=current,
                sl=p.sl,
                tp=p.tp,
                sl_room=_room(p.type, current, p.sl, stop=True),
                tp_room=_room(p.type, current, p.tp, stop=False),
                swap=lp.swap if lp else None,
                profit=lp.profit if lp else None,
                link=link,
                link_tier=tier,
                digits=digits,
                master_ticket=copy.master_ticket if copy is not None else None,
                breakeven=be_text,
                breakeven_tier=be_tier,
            )
        )
    return rows


def held_by_counts(copies_per_slave: Iterable[Sequence[OpenCopy] | None]) -> dict[int, int]:
    """Per master ticket, how many slaves hold an open copy of it. An unreadable slave adds
    nothing, which shows as a shortfall rather than as a copy that may not exist."""
    counts: dict[int, int] = {}
    for copies in copies_per_slave:
        seen = {c.master_ticket for c in copies or () if not c.resting}
        for ticket in seen:
            counts[ticket] = counts.get(ticket, 0) + 1
    return counts


def margin_gauge(snap: Snapshot) -> MarginGauge:
    used: float | None = None
    if snap.margin is not None and snap.equity:
        used = min(1.0, max(0.0, snap.margin / snap.equity)) if snap.equity > 0 else 1.0
    level = snap.margin_level if snap.margin_level else None
    if level is None:
        tier: Tier = "muted" if snap.margin is None else "good"
    elif level < MARGIN_LEVEL_DANGER:
        tier = "critical"
    elif level < MARGIN_LEVEL_WARN:
        tier = "warning"
    else:
        tier = "good"
    return MarginGauge(used_fraction=used, level_pct=level, level_tier=tier, free=snap.margin_free)


def exposure(positions: Iterable[Position] | None) -> list[Exposure]:
    """Net and gross lots per symbol, largest gross first."""
    net: dict[str, float] = {}
    gross: dict[str, float] = {}
    for p in positions or ():
        signed = p.volume if p.type is PositionType.BUY else -p.volume
        net[p.symbol] = net.get(p.symbol, 0.0) + signed
        gross[p.symbol] = gross.get(p.symbol, 0.0) + p.volume
    return sorted(
        (Exposure(s, round(net[s], 8), round(gross[s], 8)) for s in net),
        key=lambda e: (-e.gross_lots, e.symbol),
    )


def bar(fraction: float | None, width: int) -> str:
    """A text bar for a 0..1 fraction. Blank track when unknown, so it never reads as empty."""
    if fraction is None:
        return "░" * width
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "█" * filled + "░" * (width - filled)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def format_ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} ms" if value >= 10 else f"{value:.1f} ms"


def format_ping(ping_us: int | None) -> str:
    return "—" if ping_us is None or ping_us <= 0 else format_ms(ping_us / 1000.0)
