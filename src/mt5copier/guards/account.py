"""The read-only picture of one account that every guard rule is given.

Built in one place so a rule holds only its own arithmetic. Nothing here is ever changed by a
rule: they are handed a frozen copy, as the detail page is handed its statistics, because the
drain thread replaces the snapshot underneath without a lock the rules would respect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..catalog import Catalog
from ..news import Calendar
from ..protocol import Deal, PendingOrder, Position, PositionLive, Snapshot
from ..symbols import undecorate
from .params import zone_info

_BALANCE = 2
"""MT5's ``DEAL_TYPE_BALANCE``: a deposit or a withdrawal, which belongs to no trade."""
_ENTRY_IN = 0
"""MT5's ``DEAL_ENTRY_IN``: the deal that opened a position."""


@dataclass(frozen=True, slots=True)
class AccountView:
    now: float
    """Real UTC seconds."""
    balance: float | None
    equity: float | None
    margin_free: float | None
    margin: float | None
    """The margin the open positions use now."""
    currency: str
    positions: tuple[Position, ...] | None
    live: dict[int, PositionLive]
    deals: tuple[Deal, ...] | None
    deals_since: float | None
    initial_balance: float | None
    """The account size the person entered on the guards page; the firms' "initial balance"."""
    pendings: tuple[PendingOrder, ...] | None = None
    """Resting limit/stop orders, hand-placed ones included; None while they are unread."""
    sizing_risk: float | None = None
    """What this slave's own sizing settings risk on one trade right now, in money."""
    news: Calendar | None = None
    """The news calendar, given only to an account with a news rule."""
    catalog: Catalog | None = None
    """The symbol catalog, given only to an account with a rule that reads asset classes."""
    decor: tuple[str, str, tuple[tuple[str, str], ...]] | None = None
    """This slave's symbol decoration -- ``(prefix, suffix, (master, slave) renames)`` -- so a
    position's broker name can be read back to the base name the catalog speaks."""

    @classmethod
    def of(
        cls,
        snap: Snapshot,
        *,
        now: float,
        initial_balance: float | None,
        sizing_risk: float | None = None,
        news: Calendar | None = None,
        catalog: Catalog | None = None,
        decor: tuple[str, str, tuple[tuple[str, str], ...]] | None = None,
    ) -> AccountView:
        return cls(
            now=now,
            balance=snap.balance,
            equity=snap.equity,
            margin_free=snap.margin_free,
            margin=snap.margin,
            currency=snap.currency or "",
            positions=snap.open_positions,
            live={p.ticket: p for p in snap.live_positions or ()},
            deals=snap.deals,
            deals_since=snap.deals_since,
            initial_balance=initial_balance,
            pendings=snap.pending_orders,
            sizing_risk=sizing_risk,
            news=news,
            catalog=catalog,
            decor=decor,
        )

    def base_symbol(self, name: str) -> str:
        """A position's broker name read back to the base the catalog speaks: an explicit
        rename reversed first (it cannot be guessed from the name), then the configured
        prefix and suffix taken off. Without the decoration, the name as it stands -- the
        catalog's own matching is already forgiving about common decorations."""
        if self.decor is None:
            return name
        prefix, suffix, renames = self.decor
        for master, slave in renames:
            if slave.upper() == name.upper():
                return master
        return undecorate(name, prefix, suffix)

    def balance_at(self, when: float) -> float | None:
        """The balance at ``when``: today's balance less everything every later deal added.

        None when it cannot be known -- the history is unread, or does not reach back that far
        -- never a guess, since a guard working from a wrong opening balance would report a
        loss as a gain.
        """
        if self.balance is None or self.deals is None or self.deals_since is None:
            return None
        if when < self.deals_since:
            return None
        return self.balance - sum(d.net for d in self.deals if d.time >= when)

    def open_at(self, when: float) -> bool | None:
        """Whether any trade was open at ``when``; None when it cannot be told.

        Told by the trades' ids, not their times, so no broker clock comes into it: a trade open
        now, or closed since ``when``, was open then unless it was opened since. Its opening
        deal is in the history if it was, and the history covers ``when`` onwards.
        """
        if self.deals is None or self.deals_since is None or self.positions is None:
            return None
        if when < self.deals_since:
            return None
        later = [d for d in self.deals if d.time >= when and d.type != _BALANCE]
        if any(d.position == 0 for d in later):
            return None
        opened = {d.position for d in later if d.entry == _ENTRY_IN}
        closed = {d.position for d in later if d.entry != _ENTRY_IN}
        held = {p.ticket for p in self.positions} | closed
        return bool(held - opened)

    def open_pnl(self, ticket: int) -> float | None:
        """A position's floating P/L with its swap, or None if the broker did not say."""
        live = self.live.get(ticket)
        if live is None or live.profit is None:
            return None
        return live.profit + (live.swap or 0.0)


def local_day(now: float) -> str:
    """The date at ``now`` on this computer's clock: the key trade counts file under.

    Local on purpose, where every other daily thing here takes a zone: the trade-count rules
    stop the person overtrading, so their day ends at the midnight on the wall where they
    sit, not on a broker's clock or UTC's.
    """
    return datetime.fromtimestamp(now).date().isoformat()


def last_reset(now: float, at: str, zone: str) -> tuple[float, str]:
    """The most recent daily reset at or before ``now``, as UTC seconds, and the date it
    opened -- the key a recorded start-of-day value is filed under.

    Worked in the reset's own zone, so a daylight-saving change moves the reset with the
    clocks on the wall, as a firm's does.
    """
    tz = zone_info(zone)
    hours, minutes = (int(part) for part in at.split(":"))
    local = datetime.fromtimestamp(now, tz)
    day = local.date()
    start = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)
    if start > local:
        day -= timedelta(days=1)
        start = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)
    return start.timestamp(), day.isoformat()
