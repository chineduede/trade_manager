"""How many trades: at most so many placed in a day, at most so many open at once.

These two are about discipline rather than a firm's rulebook: the person asks the copier to
stop them overtrading with the app. So the day is this computer's day -- it ends at the
midnight on the wall, not on a broker's clock -- and the daily count is what the copier
itself placed: a filled market order, or a pending order that rested (a pending-fallback
chain counts once however it ends, and a refused trade counts nothing). What was traded by
hand the copier cannot stop, so the daily rule does not pretend to see it -- but how many
trades are *open* is a fact the snapshot already knows whole, so that rule counts every
position on the account, hand-opened ones included.

The counts are exact -- the copier does the counting, so no gap can jump them the way equity
jumps a money limit -- which is why neither rule takes a buffer: each acts at its cap, never
before, and lets go by itself, at midnight or when a trade closes.

The open-trades rule can be told not to count a risk-free trade: one whose stop loss sits at
or past its entry, so the market can no longer take the entry price back. The cap is there to
limit risk, and such a trade has none left to limit -- moving a stop to breakeven frees its
place for another trade. "At entry" is taken at face value: the commission a breakeven exit
still pays is not the kind of risk this cap is for, and the guard cannot know each broker's
costs anyway. A trade with no stop at all is never risk-free.
"""

from __future__ import annotations

from ...protocol import Position, PositionType
from ..account import AccountView, local_day
from ..base import Measure, Memory, Rule, register
from ..params import Param, ParamKind, ParamValue, on_breach


@register
class DailyTrades(Rule):
    type = "daily_trades"
    title = "Max trades a day"
    blurb = "caps how many trades are placed in a day"
    counts = True
    params = (
        Param(
            "limit", "Limit", ParamKind.NUMBER, 5.0, at_least=1, whole=True, after="trades a day"
        ),
        Param(
            "pendings",
            "A pending order placed",
            ParamKind.CHOICE,
            "counts",
            choices=(("counts", "counts as a trade"), ("ignored", "does not count")),
        ),
        on_breach("block_new", "warn", "block_new"),
    )

    def basis(self, p: dict[str, ParamValue]) -> str:
        return "trades the copier placed since midnight, this computer's time"

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        pendings = (
            "a pending order counts when it is placed"
            if p["pendings"] == "counts"
            else "pending orders are not counted"
        )
        return (
            f"Place at most {p['limit']:g} trades a day ({pendings}); the day ends at "
            "midnight, this computer's time. Trades made by hand are not seen."
        )

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        market, pending = memory.opens_today(local_day(view.now))
        count = market + (pending if p["pendings"] == "counts" else 0)
        limit = float(p["limit"])
        left = int(limit) - count
        standing = f"{left} left" if left > 0 else "at the cap until midnight"
        return Measure(used=float(count), limit=limit, detail=f"{count} placed today · {standing}")


def _risk_free(pos: Position) -> bool:
    """Whether this position can no longer lose: its stop sits at or past its entry -- at or
    above for a buy, at or below for a sell. No stop, no safety."""
    if not pos.has_stop:
        return False
    if pos.type == PositionType.BUY:
        return pos.sl >= pos.price_open
    return pos.sl <= pos.price_open


@register
class OpenTrades(Rule):
    type = "open_trades"
    title = "Max open trades"
    blurb = "caps how many trades are open at once"
    counts = True
    params = (
        Param(
            "limit", "Limit", ParamKind.NUMBER, 3.0, at_least=1, whole=True, after="open at once"
        ),
        Param(
            "pendings",
            "A resting pending order",
            ParamKind.CHOICE,
            "ignored",
            choices=(("ignored", "does not count"), ("counts", "counts as a trade")),
        ),
        Param(
            "risk_free",
            "A risk-free trade (stop at or past entry)",
            ParamKind.CHOICE,
            "counts",
            choices=(("counts", "counts as a trade"), ("ignored", "does not count")),
        ),
        on_breach("block_new", "warn", "block_new"),
    )

    def basis(self, p: dict[str, ParamValue]) -> str:
        if p["risk_free"] == "ignored":
            return "every position still able to lose, hand-opened ones included"
        return "every position open on the account, hand-opened ones included"

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        what = (
            "positions and resting pending orders together"
            if p["pendings"] == "counts"
            else "positions"
        )
        risk_free = (
            "; a trade whose stop sits at or past its entry is risk-free and not counted"
            if p["risk_free"] == "ignored"
            else ""
        )
        return (
            f"Hold at most {p['limit']:g} open {what} at once, "
            f"hand-opened ones included{risk_free}."
        )

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        if view.positions is None:
            return Measure(unknown="the open positions have not been read yet")
        safe = (
            sum(1 for pos in view.positions if _risk_free(pos))
            if p["risk_free"] == "ignored"
            else 0
        )
        count = len(view.positions) - safe
        detail = f"{count} open"
        if p["pendings"] == "counts":
            if view.pendings is None:
                return Measure(unknown="the resting orders have not been read yet")
            count += len(view.pendings)
            detail += f" + {len(view.pendings)} resting"
        limit = float(p["limit"])
        detail = f"{detail} of {limit:g} allowed"
        if safe:
            detail += f" · {safe} risk-free not counted"
        return Measure(used=float(count), limit=limit, detail=detail)
