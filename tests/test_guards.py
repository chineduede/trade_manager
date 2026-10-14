"""The guards, display only: parameters, the account view, each rule's arithmetic, the book."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mt5copier.guards import AccountGuards, GuardBook, GuardRule, GuardsDoc, Level
from mt5copier.guards.account import AccountView, last_reset
from mt5copier.guards.model import load, save
from mt5copier.guards.params import Param, ParamKind
from mt5copier.protocol import (
    Deal,
    Position,
    PositionLive,
    PositionType,
    Snapshot,
    WorkerState,
)

KEY = "1001@Prop-Demo"
PRAGUE = ZoneInfo("Europe/Prague")


def at(year: int, month: int, day: int, hour: int, minute: int = 0, tz: ZoneInfo = PRAGUE) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


NOW = at(2026, 10, 5, 15)
"""A Monday afternoon in Prague; the day reset at 00:00 there."""


def snap(
    *,
    balance: float = 100_000.0,
    equity: float | None = None,
    deals: tuple[Deal, ...] | None = (),
    since: float | None = NOW - 50 * 3600,
    positions: tuple[tuple[float, float], ...] = (),
    margin_free: float | None = 90_000.0,
    margin: float | None = None,
) -> Snapshot:
    """``positions`` are ``(stop, floating P/L)`` pairs."""
    opened = tuple(
        Position(
            ticket=10 + i, symbol="EURUSD", type=PositionType.BUY, volume=1.0, price_open=1.1, sl=sl
        )
        for i, (sl, _) in enumerate(positions)
    )
    live = tuple(
        PositionLive(ticket=10 + i, profit=pnl, swap=0.0) for i, (_, pnl) in enumerate(positions)
    )
    return Snapshot(
        terminal_id="T",
        state=WorkerState.CONNECTED,
        balance=balance,
        equity=balance if equity is None else equity,
        margin_free=margin_free,
        margin=margin,
        currency="USD",
        open_positions=opened,
        live_positions=live,
        deals=deals,
        deals_since=since,
    )


def deal(when: float, net: float, ticket: int = 1, position: int = 0, entry: int = 1) -> Deal:
    return Deal(ticket=ticket, time=when, type=1, entry=entry, profit=net, position=position)


HELD = ((0.0, 0.0),)
"""One trade open now and opened before the reset: the equity then cannot be known."""


def book_with(*rules: GuardRule, initial: float | None = 100_000.0, tmp: Path) -> GuardBook:
    doc = GuardsDoc(accounts={KEY: AccountGuards(initial_balance=initial, rules=rules)})
    return GuardBook(doc, state_path=tmp / "state.json")


def only(book: GuardBook, s: Snapshot, now: float = NOW, **kw: float):  # type: ignore[no-untyped-def]
    report = book.report(KEY, s, now, **kw)
    assert len(report.rules) == 1
    return report.rules[0]


# --- parameters --------------------------------------------------------------------------------


def test_number_bounds_and_junk() -> None:
    p = Param("limit", "Limit", ParamKind.NUMBER, 5.0, minimum=0, maximum=100)
    assert p.check("2.5") == (2.5, None)
    assert p.check(0)[1] is not None
    assert p.check(101)[1] is not None
    assert p.check("nan")[1] is not None
    assert p.check("five")[1] is not None


def test_time_and_zone() -> None:
    t = Param("reset", "Reset", ParamKind.TIME, "00:00")
    assert t.check("7:05") == ("07:05", None)
    assert t.check("24:00")[1] is not None
    z = Param("zone", "Zone", ParamKind.ZONE, "UTC")
    assert z.check("America/New_York") == ("America/New_York", None)
    assert z.check("Mars/Olympus")[1] is not None


def test_a_zone_can_be_an_offset_or_a_name_in_any_case() -> None:
    z = Param("zone", "Zone", ParamKind.ZONE, "UTC")
    assert z.check("utc+2") == ("UTC+2", None)
    assert z.check("GMT-5") == ("UTC-5", None)
    assert z.check("UTC+03:00") == ("UTC+3", None)
    assert z.check("gmt") == ("UTC", None)
    assert z.check("europe/athens") == ("Europe/Athens", None)
    half = z.check("UTC+5:30")[1]
    assert half is not None and "whole hours" in half
    assert z.check("UTC+15")[1] is not None


def test_an_offset_reset_is_that_many_hours_ahead_of_utc() -> None:
    # 00:00 at UTC+2 is 22:00 UTC the day before: the Etc names' reversed sign is handled.
    now = at(2026, 1, 10, 12, tz=ZoneInfo("UTC"))
    assert last_reset(now, "00:00", "UTC+2") == (
        at(2026, 1, 9, 22, tz=ZoneInfo("UTC")),
        "2026-01-10",
    )


def test_a_bad_value_is_reported_and_the_default_used() -> None:
    rule = GuardRule(type="max_drawdown", params={"limit": -3})
    params, problems = rule.resolved()
    assert params["limit"] == 10.0 and problems


def test_an_unknown_rule_type_is_kept_and_reported(tmp_path: Path) -> None:
    doc = GuardsDoc(accounts={KEY: AccountGuards(rules=(GuardRule(type="from_the_future"),))})
    save(doc, tmp_path / "g.json")
    again = load(tmp_path / "g.json")
    assert again.accounts[KEY].rules[0].type == "from_the_future"
    status = GuardBook(again, tmp_path / "s.json").report(KEY, snap(), NOW).rules[0]
    assert status.level is Level.UNKNOWN and "from_the_future" in status.sentence


def test_a_corrupt_file_is_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "g.json"
    path.write_text("{not json", encoding="utf-8")
    assert load(path).accounts == {}
    assert not path.exists()


# --- time and history --------------------------------------------------------------------------


def test_last_reset_is_today_or_yesterday() -> None:
    assert last_reset(NOW, "00:00", "Europe/Prague") == (at(2026, 10, 5, 0), "2026-10-05")
    assert last_reset(NOW, "17:00", "Europe/Prague") == (at(2026, 10, 4, 17), "2026-10-04")


def test_last_reset_follows_daylight_saving() -> None:
    """Clocks went back in Prague on 2026-10-25: midnight is still midnight on the wall."""
    after = at(2026, 10, 26, 12)
    reset, _ = last_reset(after, "00:00", "Europe/Prague")
    assert datetime.fromtimestamp(reset, PRAGUE).hour == 0
    assert datetime.fromtimestamp(reset, UTC).hour == 23


def test_balance_at_takes_off_later_deals_only() -> None:
    view = AccountView.of(
        snap(balance=99_000.0, deals=(deal(NOW - 30 * 3600, 500.0), deal(NOW - 3600, -1_000.0))),
        now=NOW,
        initial_balance=None,
    )
    assert view.balance_at(NOW - 2 * 3600) == 100_000.0
    assert view.balance_at(NOW - 40 * 3600) == 99_500.0


def test_balance_at_is_unknown_without_history_or_before_it() -> None:
    assert AccountView.of(snap(deals=None), now=NOW, initial_balance=None).balance_at(NOW) is None
    view = AccountView.of(snap(), now=NOW, initial_balance=None)
    assert view.balance_at(NOW - 60 * 3600) is None


# --- daily loss --------------------------------------------------------------------------------


def daily(**params: float | str) -> GuardRule:
    return GuardRule(type="daily_loss", params={"zone": "Europe/Prague", **params})


def test_daily_loss_counts_closed_and_floating_losses_since_the_reset(tmp_path: Path) -> None:
    s = snap(balance=98_500.0, equity=98_000.0, deals=(deal(at(2026, 10, 5, 9), -1_500.0),))
    status = only(book_with(daily(limit=5), tmp=tmp_path), s)
    assert status.used == pytest.approx(2_000.0)
    assert status.limit == pytest.approx(5_000.0)
    assert status.level is Level.OK
    assert "95,000.00" in status.detail


def test_daily_loss_ignores_yesterdays_deals(tmp_path: Path) -> None:
    s = snap(balance=99_000.0, deals=(deal(at(2026, 10, 4, 22), -1_000.0),))
    assert only(book_with(daily(limit=5), tmp=tmp_path), s).used == 0.0


def test_daily_loss_near_and_breached(tmp_path: Path) -> None:
    book = book_with(daily(limit=1000, unit="money"), tmp=tmp_path)
    assert only(book, snap(equity=99_150.0)).level is Level.NEAR
    assert only(book, snap(equity=99_000.0)).level is Level.BREACHED


def test_daily_loss_percent_of_day_start(tmp_path: Path) -> None:
    s = snap(balance=50_000.0)
    status = only(book_with(daily(limit=4, percent_of="day_start"), tmp=tmp_path), s)
    assert status.limit == pytest.approx(2_000.0)


def test_daily_loss_needs_history(tmp_path: Path) -> None:
    status = only(book_with(daily(limit=5), tmp=tmp_path), snap(deals=None))
    assert status.level is Level.UNKNOWN and "history" in status.detail


def test_daily_loss_on_equity_waits_for_a_watched_reset(tmp_path: Path) -> None:
    book = book_with(daily(limit=5, basis="equity"), tmp=tmp_path)
    unknown = only(book, snap(positions=HELD))
    assert unknown.level is Level.UNKNOWN
    # Says why it cannot be known, and what to choose for an estimate today.
    assert "00:00 Europe/Prague" in unknown.detail and "not running then" in unknown.detail
    assert "the balance at the reset" in unknown.detail

    before, after = at(2026, 10, 6, 0, 0) - 20, at(2026, 10, 6, 0, 0) + 1
    book.observe(KEY, snap(equity=101_000.0), before)
    book.observe(KEY, snap(equity=101_200.0), after)
    status = only(book, snap(equity=100_200.0), now=after + 60)
    assert status.used == pytest.approx(1_000.0)

    # Recorded on disk, so a restart later that day still knows it.
    again = GuardBook(book.doc, state_path=tmp_path / "state.json")
    assert only(again, snap(equity=100_200.0), now=after + 600).used == pytest.approx(1_000.0)


def test_a_reset_slept_through_is_not_recorded(tmp_path: Path) -> None:
    book = book_with(daily(limit=5, basis="equity"), tmp=tmp_path)
    book.observe(KEY, snap(positions=HELD), at(2026, 10, 5, 23, 0))
    book.observe(KEY, snap(positions=HELD), at(2026, 10, 6, 1, 0))
    status = only(book, snap(positions=HELD), now=at(2026, 10, 6, 1, 1))
    assert status.level is Level.UNKNOWN


def test_higher_basis_falls_back_to_balance_and_says_so(tmp_path: Path) -> None:
    s = snap(equity=99_000.0, positions=HELD)
    status = only(book_with(daily(limit=5, basis="higher"), tmp=tmp_path), s)
    assert status.used == pytest.approx(1_000.0) and "not recorded" in status.detail


def test_with_nothing_open_at_the_reset_its_equity_was_its_balance(tmp_path: Path) -> None:
    # Closed a trade opened and closed since the reset, and holds nothing: nothing was open
    # at 00:00, so the day started at the balance then, unrecorded or not.
    since = (
        deal(at(2026, 10, 5, 8), 0.0, ticket=1, position=50, entry=0),
        deal(at(2026, 10, 5, 9), -1_500.0, ticket=2, position=50),
    )
    for basis in ("equity", "higher"):
        status = only(
            book_with(daily(limit=5, basis=basis), tmp=tmp_path),
            snap(balance=98_500.0, equity=98_000.0, deals=since),
        )
        assert status.level is not Level.UNKNOWN, basis
        assert status.used == pytest.approx(2_000.0)
        assert "100,000.00" in status.detail and "nothing was open" in status.detail


def test_a_trade_opened_before_the_reset_leaves_its_equity_unknown(tmp_path: Path) -> None:
    book = book_with(daily(limit=5, basis="equity"), tmp=tmp_path)
    # Closed since the reset, but its opening is not in the history after it.
    closed = (deal(at(2026, 10, 5, 9), -1_500.0, position=50),)
    assert only(book, snap(deals=closed)).level is Level.UNKNOWN
    # Open now, and opened since the reset: nothing was open then.
    opened = (deal(at(2026, 10, 5, 9), 0.0, position=10, entry=0),)
    assert only(book, snap(deals=opened, positions=HELD)).level is not Level.UNKNOWN
    # A history that cannot say which trade a deal belongs to tells nothing.
    assert only(book, snap(deals=(deal(at(2026, 10, 5, 9), -1.0),))).level is Level.UNKNOWN
    # Nor does one not read.
    assert only(book, snap(deals=None)).level is Level.UNKNOWN


# --- the others --------------------------------------------------------------------------------


def test_max_drawdown_from_the_initial_balance(tmp_path: Path) -> None:
    book = book_with(GuardRule(type="max_drawdown", params={"limit": 10}), tmp=tmp_path)
    status = only(book, snap(balance=95_000.0, equity=94_000.0))
    assert (status.used, status.limit) == (pytest.approx(6_000.0), pytest.approx(10_000.0))


def test_max_drawdown_needs_the_initial_balance(tmp_path: Path) -> None:
    book = book_with(GuardRule(type="max_drawdown"), initial=None, tmp=tmp_path)
    status = only(book, snap())
    assert status.level is Level.UNKNOWN and "initial balance" in status.detail


def test_floating_loss_net_and_losers_only(tmp_path: Path) -> None:
    s = snap(positions=((1.0, -800.0), (1.0, 300.0)))
    net = GuardRule(type="floating_loss", params={"limit": 1000, "unit": "money"})
    losers = GuardRule(
        type="floating_loss", params={"limit": 1000, "unit": "money", "counts": "losers"}
    )
    assert only(book_with(net, tmp=tmp_path), s).used == pytest.approx(500.0)
    assert only(book_with(losers, tmp=tmp_path), s).used == pytest.approx(800.0)


def test_margin_per_trade_shows_the_limit_now(tmp_path: Path) -> None:
    book = book_with(GuardRule(type="margin_per_trade", params={"limit": 10}), tmp=tmp_path)
    status = only(book, snap(margin_free=50_000.0))
    assert status.limit == pytest.approx(5_000.0) and status.level is Level.OK


def test_total_margin_counts_the_margin_in_use(tmp_path: Path) -> None:
    rule = GuardRule(type="total_margin", params={"limit": 30, "percent_of": "balance"})
    book = book_with(rule, tmp=tmp_path)
    status = only(book, snap(balance=10_000.0, margin=2_000.0))
    assert (status.used, status.limit) == (pytest.approx(2_000.0), pytest.approx(3_000.0))
    assert "may add up to 1,000.00" in status.detail
    assert status.basis == "30% of the balance"
    verdict = book.verdict(KEY, snap(balance=10_000.0, margin=2_000.0), NOW)
    assert verdict.max_total_margin is not None
    assert verdict.max_total_margin.amount == pytest.approx(3_000.0)
    assert verdict.max_margin is None


def test_risk_per_trade_against_the_slaves_own_sizing(tmp_path: Path) -> None:
    book = book_with(GuardRule(type="risk_per_trade", params={"limit": 1}), tmp=tmp_path)
    assert only(book, snap(), sizing_risk=500.0).level is Level.OK
    assert only(book, snap(), sizing_risk=1_500.0).level is Level.BREACHED


def test_stop_required_counts_bare_positions(tmp_path: Path) -> None:
    book = book_with(GuardRule(type="stop_required"), tmp=tmp_path)
    assert only(book, snap(positions=((1.0, 0.0),))).level is Level.OK
    status = only(book, snap(positions=((1.0, 0.0), (0.0, 0.0))))
    assert status.level is Level.NEAR and "1 open position has no stop" in status.detail


# --- the report --------------------------------------------------------------------------------


def test_a_switched_off_set_measures_nothing(tmp_path: Path) -> None:
    doc = GuardsDoc(
        accounts={KEY: AccountGuards(enabled=False, rules=(GuardRule(type="max_drawdown"),))}
    )
    report = GuardBook(doc, tmp_path / "s.json").report(KEY, snap(), NOW)
    assert report.rules[0].level is Level.OFF and report.worst is None


def test_worst_is_the_most_serious_then_the_fullest(tmp_path: Path) -> None:
    rules = (
        GuardRule(id="a", type="max_drawdown", params={"limit": 2000, "unit": "money"}),
        GuardRule(id="b", type="daily_loss", params={"limit": 1500, "unit": "money"}),
    )
    report = book_with(*rules, tmp=tmp_path).report(KEY, snap(equity=99_000.0), NOW)
    assert report.worst is not None and report.worst.rule_id == "b"


def test_an_account_with_no_guards_reports_nothing(tmp_path: Path) -> None:
    report = GuardBook(GuardsDoc(), tmp_path / "s.json").report("other@x", snap(), NOW)
    assert report.rules == () and report.worst is None
