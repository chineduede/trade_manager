"""No new trades around high-impact news.

Firms differ in the window, the impact that counts, and whether only the instruments the news
moves are restricted or every trade -- so each is a parameter, never a firm's preset. Like the
other rules about a new trade, it never touches what the slave already holds: a close, a
partial or a stop move is copied through the news as at any other time.

Which symbols the news moves is read from their names (:func:`mt5copier.news.symbol_currencies`);
a symbol whose name cannot be read is treated as moved by any news, since guessing "not
affected" is the guess that breaks a firm's rule.
"""

from __future__ import annotations

import time

from ...news import HIGH, MEDIUM, Calendar, NewsEvent
from ..account import AccountView
from ..base import Level, Measure, Memory, Rule, register
from ..params import Param, ParamKind, ParamValue, on_breach

ALL = "*"
"""In a set of held currencies: every new trade, whatever its symbol."""


@register
class NewsWindow(Rule):
    type = "news"
    title = "High-impact news"
    blurb = "no new trades around big news"
    news = True
    params = (
        Param(
            "impact",
            "News that counts",
            ParamKind.CHOICE,
            "high",
            choices=(("high", "high impact only"), ("medium", "high and medium impact")),
        ),
        Param("before", "From", ParamKind.NUMBER, 2.0, minimum=0, maximum=240, after="min before"),
        Param("after", "Until", ParamKind.NUMBER, 2.0, minimum=0, maximum=240, after="min after"),
        Param(
            "affects",
            "Which new trades",
            ParamKind.CHOICE,
            "currency",
            choices=(
                ("currency", "only those in the news currency"),
                ("all", "every new trade"),
            ),
        ),
        Param(
            "if_unknown",
            "If the calendar cannot be read",
            ParamKind.CHOICE,
            "hold",
            choices=(("hold", "take no new trades"), ("copy", "copy as usual")),
        ),
        on_breach("decline", "warn", "decline"),
    )

    def basis(self, p: dict[str, ParamValue]) -> str:
        return f"{float(p['before']):g} min before to {float(p['after']):g} min after"

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        impact = "high-impact" if p["impact"] == "high" else "high- or medium-impact"
        which = "in its currency" if p["affects"] == "currency" else "at all"
        return (
            f"No new trade {which} from {float(p['before']):g} min before {impact} news "
            f"until {float(p['after']):g} min after it."
        )

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        calendar = view.news
        if calendar is None:
            return Measure(unknown="the news calendar has not been asked for yet")
        held, why = self.held(p, calendar, view.now)
        if held:
            return Measure(level=Level.NEAR, detail=why)
        if calendar.events is None:
            return Measure(unknown=calendar.said)
        upcoming = [e for e in counted(p, calendar.events) if e.when > view.now]
        if not upcoming:
            return Measure(level=Level.OK, detail="no more news this week")
        e = upcoming[0]
        return Measure(level=Level.OK, detail=f"next: {e.currency} {e.title}, {when(e.when)}")

    def held(
        self, p: dict[str, ParamValue], calendar: Calendar, now: float
    ) -> tuple[frozenset[str], str]:
        """The currencies whose new trades are held now (``ALL`` for every one), and why."""
        if calendar.events is None:
            if p["if_unknown"] == "hold":
                return frozenset({ALL}), f"{calendar.said}; new trades held until it can be"
            return frozenset(), ""
        before, after = float(p["before"]) * 60, float(p["after"]) * 60
        now_on = [
            e for e in counted(p, calendar.events) if e.when - before <= now <= e.when + after
        ]
        if not now_on:
            return frozenset(), ""
        last = max(e.when for e in now_on) + after
        names = "; ".join(f"{e.currency} {e.title} at {clock(e.when)}" for e in now_on[:3])
        # The feed files news for no one currency, such as an OPEC meeting, under "All".
        if p["affects"] == "all" or any(e.currency == "ALL" for e in now_on):
            return frozenset({ALL}), f"{names}: no new trades until {clock(last)}"
        currencies = frozenset(e.currency for e in now_on)
        listed = ", ".join(sorted(currencies))
        return currencies, f"{names}: no new {listed} trades until {clock(last)}"


def counted(p: dict[str, ParamValue], events: tuple[NewsEvent, ...]) -> list[NewsEvent]:
    impacts = {HIGH} if p["impact"] == "high" else {HIGH, MEDIUM}
    return [e for e in events if e.impact in impacts]


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def when(ts: float) -> str:
    """``14:30`` today, ``Tue 14:30`` later in the week, in local time."""
    if time.strftime("%Y%m%d", time.localtime(ts)) == time.strftime("%Y%m%d"):
        return clock(ts)
    return time.strftime("%a %H:%M", time.localtime(ts))
