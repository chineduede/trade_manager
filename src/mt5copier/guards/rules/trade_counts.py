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
"""

from __future__ import annotations

from ..account import AccountView, local_day
from ..base import Measure, Memory, Rule, register
from ..params import Param, ParamKind, ParamValue, on_breach


@register
class DailyTrades(Rule):
    type = "daily_trades"
    title = "Max trades a day"
    blurb = "caps how many trades are placed in a day"
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


@register
class OpenTrades(Rule):
    type = "open_trades"
    title = "Max open trades"
    blurb = "caps how many trades are open at once"
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
        on_breach("block_new", "warn", "block_new"),
    )

    def basis(self, p: dict[str, ParamValue]) -> str:
        return "every position open on the account, hand-opened ones included"

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        what = (
            "positions and resting pending orders together"
            if p["pendings"] == "counts"
            else "positions"
        )
        return f"Hold at most {p['limit']:g} open {what} at once, hand-opened ones included."

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        if view.positions is None:
            return Measure(unknown="the open positions have not been read yet")
        count = len(view.positions)
        detail = f"{count} open"
        if p["pendings"] == "counts":
            if view.pendings is None:
                return Measure(unknown="the resting orders have not been read yet")
            count += len(view.pendings)
            detail = f"{len(view.positions)} open + {len(view.pendings)} resting"
        limit = float(p["limit"])
        return Measure(used=float(count), limit=limit, detail=f"{detail} of {limit:g} allowed")
