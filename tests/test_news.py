"""The news guard: reading the calendar, which symbols news moves, and new trades declined."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_guards import KEY, NOW, snap
from test_guards_enforce import _slave_kw
from test_reconcile import pos, slave

from mt5copier import news as news_mod
from mt5copier.guards import REGISTRY, AccountGuards, GuardBook, GuardRule, GuardsDoc, Level
from mt5copier.news import Calendar, NewsCalendar, parse_feed, symbol_currencies
from mt5copier.protocol import ActionKind
from mt5copier.reconcile import GuardGate, SkipReason, reconcile_slave

NEWS = REGISTRY["news"]


def feed_item(minutes: float, currency: str = "USD", impact: str = "High", title: str = "CPI m/m"):
    """One feed event ``minutes`` from NOW, in the feed's own form (New York offset)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    when = datetime.fromtimestamp(NOW + minutes * 60, ZoneInfo("America/New_York"))
    return {"title": title, "country": currency, "date": when.isoformat(), "impact": impact}


def calendar(*items: dict[str, str]) -> Calendar:
    return Calendar(parse_feed(list(items)))


def params(**given: Any) -> dict[str, Any]:
    resolved, problems = GuardRule(type="news", params=given).resolved()
    assert problems == []
    return resolved


# --- the feed ----------------------------------------------------------------------------------


def test_the_feed_is_read_in_time_order_and_bad_items_skipped() -> None:
    events = parse_feed(
        [feed_item(30, "EUR"), {"title": "no date"}, "junk", feed_item(-10, "usd", "Medium")]
    )
    assert [(e.currency, e.impact) for e in events] == [("USD", "Medium"), ("EUR", "High")]
    assert events[0].when == pytest.approx(NOW - 600)


def test_a_feed_with_nothing_readable_is_an_error() -> None:
    with pytest.raises(ValueError):
        parse_feed([{"title": "no date"}])
    with pytest.raises(ValueError):
        parse_feed({"not": "a list"})
    assert parse_feed([]) == ()


@pytest.mark.parametrize(
    ("symbol", "currencies"),
    [
        ("EURUSD", {"EUR", "USD"}),
        ("EURUSD.r", {"EUR", "USD"}),
        ("m.GBPJPYp", {"GBP", "JPY"}),
        ("XAUUSD", {"XAU", "USD"}),
        ("US30", {"USD"}),
        ("US30.cash", {"USD"}),
        ("NAS100", {"USD"}),
        ("GER40", {"EUR"}),
        ("UK100", {"GBP"}),
        ("BTCUSD", {"USD"}),
        ("USOIL", {"USD"}),
        ("COCOA", None),
    ],
)
def test_which_currencies_a_symbol_trades(symbol: str, currencies: set[str] | None) -> None:
    found = symbol_currencies(symbol)
    assert (set(found) if found is not None else None) == currencies


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def test_the_calendar_is_fetched_only_once_wanted_and_kept_on_disk(tmp_path: Path) -> None:
    clock, calls = Clock(), []

    def fetch() -> object:
        calls.append(clock.now)
        return [feed_item(30)]

    cal = NewsCalendar(tmp_path / "news.json", fetch=fetch, clock=clock)
    assert not cal.due(), "nothing is fetched until a rule asks"
    first = cal.current()
    assert first.events is None and "not been fetched yet" in first.said
    assert cal.due() and cal.refresh()
    current = cal.current().events
    assert current is not None and len(current) == 1
    assert not cal.due(), "fetched; the next is in an hour"

    clock.now += news_mod.REFRESH_S
    assert cal.due()

    again = NewsCalendar(tmp_path / "news.json", fetch=fetch, clock=clock)
    assert again.current().events == current, "a restart reads the kept copy"


def test_a_failed_fetch_keeps_the_last_copy_until_it_is_a_day_old(tmp_path: Path) -> None:
    clock = Clock()
    answers: list[object] = [[feed_item(30)]]

    def fetch() -> object:
        answer = answers.pop(0) if answers else None
        if answer is None:
            raise OSError("no route to host")
        return answer

    cal = NewsCalendar(tmp_path / "news.json", fetch=fetch, clock=clock)
    cal.current()
    assert cal.refresh()
    clock.now += news_mod.REFRESH_S
    assert not cal.refresh()
    assert cal.current().events is not None, "still the last week fetched"
    clock.now += news_mod.FRESH_S
    gone = cal.current()
    assert gone.events is None and "no route to host" in gone.said


# --- the rule ----------------------------------------------------------------------------------


def test_new_trades_in_the_news_currency_are_held_through_the_window() -> None:
    cal = calendar(
        feed_item(1, "USD", title="Non-Farm Employment Change"), feed_item(1, "JPY", "Low")
    )
    held, why = NEWS.held(params(), cal, NOW)  # type: ignore[attr-defined]
    assert held == {"USD"}
    assert "USD Non-Farm Employment Change" in why and "no new USD trades until" in why

    assert NEWS.held(params(), cal, NOW - 120)[0] == set(), "before the window"  # type: ignore[attr-defined]
    assert NEWS.held(params(), cal, NOW + 180)[0] == {"USD"}  # type: ignore[attr-defined]
    assert NEWS.held(params(), cal, NOW + 181)[0] == set(), "after it"  # type: ignore[attr-defined]


def test_medium_impact_and_every_trade_are_choices() -> None:
    cal = calendar(feed_item(0, "EUR", "Medium"))
    assert NEWS.held(params(), cal, NOW)[0] == set()  # type: ignore[attr-defined]
    assert NEWS.held(params(impact="medium"), cal, NOW)[0] == {"EUR"}  # type: ignore[attr-defined]
    assert NEWS.held(params(impact="medium", affects="all"), cal, NOW)[0] == {"*"}  # type: ignore[attr-defined]


def test_news_for_all_currencies_holds_every_new_trade() -> None:
    cal = calendar(feed_item(0, "All", title="OPEC-JMMC Meetings"))
    assert NEWS.held(params(), cal, NOW)[0] == {"*"}  # type: ignore[attr-defined]


def test_an_unreadable_calendar_holds_new_trades_unless_set_to_copy() -> None:
    unknown = Calendar(None, "the news calendar could not be read: timed out")
    held, why = NEWS.held(params(), unknown, NOW)  # type: ignore[attr-defined]
    assert held == {"*"} and "timed out" in why
    assert NEWS.held(params(if_unknown="copy"), unknown, NOW)[0] == set()  # type: ignore[attr-defined]


class FixedCalendar(NewsCalendar):
    def __init__(self, cal: Calendar) -> None:
        self._fixed = cal

    def current(self) -> Calendar:
        return self._fixed


def book(cal: Calendar, tmp: Path, **given: Any) -> GuardBook:
    rule = GuardRule(id="nw", type="news", params=given)
    doc = GuardsDoc(accounts={KEY: AccountGuards(rules=(rule,))})
    return GuardBook(doc, state_path=tmp / "state.json", calendar=FixedCalendar(cal))


def test_the_verdict_carries_the_held_currencies_and_never_holds_the_slave(tmp_path: Path) -> None:
    b = book(calendar(feed_item(0, "GBP")), tmp_path)
    v = b.verdict(KEY, snap(), NOW)
    assert v.news == {"GBP"} and v.hold is None
    status = b.report(KEY, snap(), NOW).rules[0]
    assert status.level is Level.NEAR and "no new GBP trades" in status.detail

    quiet = book(calendar(feed_item(90, "GBP", title="GDP m/m")), tmp_path)
    assert quiet.verdict(KEY, snap(), NOW).news == frozenset()
    assert "next: GBP GDP m/m" in quiet.report(KEY, snap(), NOW).rules[0].detail


def test_a_rule_set_to_warn_only_warns(tmp_path: Path) -> None:
    v = book(calendar(feed_item(0)), tmp_path, on_breach="warn").verdict(KEY, snap(), NOW)
    assert v.news == frozenset() and len(v.warnings) == 1


# --- the gate ------------------------------------------------------------------------------------


def _open(gate: GuardGate, symbol: str) -> SkipReason | None:
    (action,) = reconcile_slave([pos(symbol=symbol)], [], slave(**_slave_kw()), gate=gate)
    assert action.kind is ActionKind.OPEN
    return action.skip_reason


def test_a_trade_in_a_held_currency_is_declined_and_others_copied() -> None:
    usd = GuardGate(news=frozenset({"USD"}))
    assert _open(usd, "EURUSD") is SkipReason.GUARD_NEWS
    assert _open(usd, "US30") is SkipReason.GUARD_NEWS
    assert _open(usd, "EURGBP") is None
    assert _open(usd, "COCOA") is SkipReason.GUARD_NEWS, "a name it cannot read is not risked"
    assert _open(GuardGate(news=frozenset({"*"})), "EURGBP") is SkipReason.GUARD_NEWS
    assert _open(GuardGate(), "EURUSD") is None


# --- the fetch ladder ----------------------------------------------------------------------------
# The feed's blocking has shifted twice (User-Agent, then the TLS fingerprint itself), so the
# fetch fails down a ladder of strategies. What is pinned here is the ladder, not the network.


def test_the_fetch_falls_through_to_the_first_strategy_that_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reset() -> str:
        raise OSError("[WinError 10054] An existing connection was forcibly closed")

    monkeypatch.setattr(
        news_mod, "_STRATEGIES", (("a", reset), ("b", lambda: '[{"x": 1}]'), ("c", reset))
    )
    assert news_mod._fetch_feed() == [{"x": 1}]


def test_when_every_strategy_fails_the_first_error_is_the_one_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reset() -> str:
        raise OSError("the real refusal")

    def no_curl() -> str:
        raise OSError("curl is not on this machine")

    monkeypatch.setattr(news_mod, "_STRATEGIES", (("a", reset), ("b", no_curl)))
    with pytest.raises(OSError, match="the real refusal"):
        news_mod._fetch_feed()
