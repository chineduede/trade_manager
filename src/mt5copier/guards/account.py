"""The read-only picture of one account that every guard rule is given.

Built in one place so a rule holds only its own arithmetic. Nothing here is ever changed by a
rule: they are handed a frozen copy, as the detail page is handed its statistics, because the
drain thread replaces the snapshot underneath without a lock the rules would respect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..news import Calendar
from ..protocol import Deal, Position, PositionLive, Snapshot


@dataclass(frozen=True, slots=True)
class AccountView:
    now: float
    """Real UTC seconds."""
    balance: float | None
    equity: float | None
    margin_free: float | None
    currency: str
    positions: tuple[Position, ...] | None
    live: dict[int, PositionLive]
    deals: tuple[Deal, ...] | None
    deals_since: float | None
    initial_balance: float | None
    """The account size the person entered on the guards page; the firms' "initial balance"."""
    sizing_risk: float | None = None
    """What this slave's own sizing settings risk on one trade right now, in money."""
    news: Calendar | None = None
    """The news calendar, given only to an account with a news rule."""

    @classmethod
    def of(
        cls,
        snap: Snapshot,
        *,
        now: float,
        initial_balance: float | None,
        sizing_risk: float | None = None,
        news: Calendar | None = None,
    ) -> AccountView:
        return cls(
            now=now,
            balance=snap.balance,
            equity=snap.equity,
            margin_free=snap.margin_free,
            currency=snap.currency or "",
            positions=snap.open_positions,
            live={p.ticket: p for p in snap.live_positions or ()},
            deals=snap.deals,
            deals_since=snap.deals_since,
            initial_balance=initial_balance,
            sizing_risk=sizing_risk,
            news=news,
        )

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

    def open_pnl(self, ticket: int) -> float | None:
        """A position's floating P/L with its swap, or None if the broker did not say."""
        live = self.live.get(ticket)
        if live is None or live.profit is None:
            return None
        return live.profit + (live.swap or 0.0)


def last_reset(now: float, at: str, zone: str) -> tuple[float, str]:
    """The most recent daily reset at or before ``now``, as UTC seconds, and the date it
    opened -- the key a recorded start-of-day value is filed under.

    Worked in the reset's own zone, so a daylight-saving change moves the reset with the
    clocks on the wall, as a firm's does.
    """
    tz = ZoneInfo(zone)
    hours, minutes = (int(part) for part in at.split(":"))
    local = datetime.fromtimestamp(now, tz)
    day = local.date()
    start = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)
    if start > local:
        day -= timedelta(days=1)
        start = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)
    return start.timestamp(), day.isoformat()
