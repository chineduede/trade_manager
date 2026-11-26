"""The trade-count rules: so many placed in a day, so many open at once.

The daily count is the copier's own (``GuardBook.record_open``), keyed to this computer's
day; the open count is read off the snapshot, hand-opened positions included. These tests
record and measure at the same timestamps, so they pass in any machine time zone.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from test_guards import KEY, NOW, book_with, only, snap

from mt5copier.guards import GuardBook, GuardRule
from mt5copier.guards.base import Level
from mt5copier.protocol import OrderType, PendingOrder, Position, PositionType
from mt5copier.shared_db import SharedDb

DAY = 86_400.0


def daily(limit: float = 5, pendings: str = "counts", on_breach: str = "block_new") -> GuardRule:
    return GuardRule(
        type="daily_trades",
        params={"limit": limit, "pendings": pendings, "on_breach": on_breach},
    )


def open_rule(limit: float = 3, pendings: str = "ignored", risk_free: str = "counts") -> GuardRule:
    return GuardRule(
        type="open_trades",
        params={"limit": limit, "pendings": pendings, "risk_free": risk_free},
    )


def resting(n: int = 1) -> tuple[PendingOrder, ...]:
    return tuple(
        PendingOrder(
            ticket=100 + i, symbol="EURUSD", type=OrderType.BUY_LIMIT, volume=1.0, price_open=1.09
        )
        for i in range(n)
    )


# --- max trades a day ---------------------------------------------------------------------------


def test_nothing_placed_yet_is_comfortable(tmp_path: Path) -> None:
    status = only(book_with(daily(), tmp=tmp_path), snap())
    assert status.level is Level.OK and (status.used, status.limit) == (0.0, 5.0)


def test_placements_count_and_the_cap_holds_back(tmp_path: Path) -> None:
    b = book_with(daily(limit=3), tmp=tmp_path)
    for _ in range(2):
        b.record_open(KEY, pending=False, now=NOW)
    assert b.verdict(KEY, snap(), NOW).hold is None, "under the cap, trades flow"
    b.record_open(KEY, pending=False, now=NOW)
    hold = b.verdict(KEY, snap(), NOW).hold
    assert hold is not None and "Max trades a day" in hold


def test_pending_placements_follow_the_toggle(tmp_path: Path) -> None:
    b = book_with(daily(pendings="ignored"), tmp=tmp_path)
    b.record_open(KEY, pending=False, now=NOW)
    b.record_open(KEY, pending=True, now=NOW)
    b.record_open(KEY, pending=True, now=NOW)
    assert only(b, snap()).used == 1.0, "a placed pending is not counted when ignored"

    counted = book_with(daily(pendings="counts"), tmp=tmp_path / "b")
    counted.record_open(KEY, pending=False, now=NOW)
    counted.record_open(KEY, pending=True, now=NOW)
    assert only(counted, snap()).used == 2.0


def test_a_pending_that_fills_counts_even_when_ignored(tmp_path: Path) -> None:
    b = book_with(daily(pendings="ignored"), tmp=tmp_path)
    b.record_open(KEY, pending=True, now=NOW)
    assert only(b, snap()).used == 0.0, "resting, it is not yet a trade"
    b.record_fill(KEY, now=NOW)
    assert only(b, snap()).used == 1.0, "filled, it is one"


def test_a_fill_is_never_counted_twice_when_pendings_count(tmp_path: Path) -> None:
    b = book_with(daily(pendings="counts"), tmp=tmp_path)
    b.record_open(KEY, pending=True, now=NOW)
    b.record_fill(KEY, now=NOW)
    assert only(b, snap()).used == 1.0, "the fill moves the count, it does not add one"


def test_a_pending_placed_yesterday_counts_on_the_day_it_fills(tmp_path: Path) -> None:
    b = book_with(daily(pendings="ignored"), tmp=tmp_path)
    b.record_open(KEY, pending=True, now=NOW)
    b.record_fill(KEY, now=NOW + DAY)
    assert only(b, snap(), now=NOW + DAY).used == 1.0, "the trade happened on the fill's day"


def test_a_fill_survives_a_restart(tmp_path: Path) -> None:
    b = book_with(daily(limit=1, pendings="ignored"), tmp=tmp_path)
    b.record_open(KEY, pending=True, now=NOW)
    b.record_fill(KEY, now=NOW)
    again = GuardBook(b.doc, SharedDb(tmp_path / "state.db"))
    assert again.verdict(KEY, snap(), NOW).hold is not None


def test_the_day_ends_at_this_computers_midnight(tmp_path: Path) -> None:
    b = book_with(daily(limit=1), tmp=tmp_path)
    b.record_open(KEY, pending=False, now=NOW)
    assert b.verdict(KEY, snap(), NOW).hold is not None
    assert b.verdict(KEY, snap(), NOW + DAY).hold is None, "a new day starts at zero"
    b.record_open(KEY, pending=False, now=NOW + DAY)
    assert only(b, snap(), now=NOW + DAY).used == 1.0, "the first trade of a day resets the count"


def test_the_count_survives_a_restart(tmp_path: Path) -> None:
    b = book_with(daily(limit=2), tmp=tmp_path)
    b.record_open(KEY, pending=False, now=NOW)
    b.record_open(KEY, pending=False, now=NOW)
    again = GuardBook(b.doc, SharedDb(tmp_path / "state.db"))
    assert again.verdict(KEY, snap(), NOW).hold is not None


def test_set_to_warn_it_warns_and_holds_nothing(tmp_path: Path) -> None:
    b = book_with(daily(limit=1, on_breach="warn"), tmp=tmp_path)
    b.record_open(KEY, pending=False, now=NOW)
    v = b.verdict(KEY, snap(), NOW)
    assert v.hold is None and v.warnings


def test_a_count_limit_must_be_a_whole_number() -> None:
    params, problems = daily(limit=2.5).resolved()
    assert problems and params["limit"] == 5.0, "the bad value is reported, the default used"


# --- max open trades ----------------------------------------------------------------------------


def test_open_positions_count_and_the_cap_holds_back(tmp_path: Path) -> None:
    b = book_with(open_rule(limit=3), tmp=tmp_path)
    two = snap(positions=((0.0, 0.0), (0.0, 0.0)))
    assert b.verdict(KEY, two, NOW).hold is None
    three = snap(positions=((0.0, 0.0), (0.0, 0.0), (0.0, 0.0)))
    hold = b.verdict(KEY, three, NOW).hold
    assert hold is not None and "Max open trades" in hold
    assert b.verdict(KEY, two, NOW).hold is None, "a closed trade frees its place at once"


def test_resting_orders_follow_the_toggle(tmp_path: Path) -> None:
    held = replace(snap(positions=((0.0, 0.0),)), pending_orders=resting(2))
    assert only(book_with(open_rule(), tmp=tmp_path), held).used == 1.0
    status = only(book_with(open_rule(pendings="counts"), tmp=tmp_path / "b"), held)
    assert status.used == 3.0 and "1 open + 2 resting" in status.detail


def test_risk_free_trades_follow_the_toggle(tmp_path: Path) -> None:
    held = snap(positions=((1.15, 0.0), (1.1, 0.0), (1.05, 0.0), (0.0, 0.0)))
    assert only(book_with(open_rule(limit=4), tmp=tmp_path), held).used == 4.0, (
        "counted like any other trade unless told otherwise"
    )
    status = only(book_with(open_rule(limit=4, risk_free="ignored"), tmp=tmp_path / "b"), held)
    assert status.used == 2.0, "a stop at or past entry frees its place; no stop never does"
    assert "2 risk-free not counted" in status.detail


def test_a_rule_saved_before_the_option_existed_counts_everything(tmp_path: Path) -> None:
    old = GuardRule(type="open_trades", params={"limit": 2, "pendings": "ignored"})
    held = snap(positions=((1.15, 0.0), (1.1, 0.0)))
    assert only(book_with(old, tmp=tmp_path), held).used == 2.0


def test_a_breakeven_stop_lets_another_trade_through(tmp_path: Path) -> None:
    b = book_with(open_rule(limit=2, risk_free="ignored"), tmp=tmp_path)
    at_risk = snap(positions=((1.05, 0.0), (1.08, 0.0)))
    assert b.verdict(KEY, at_risk, NOW).hold is not None, "two still at risk is the cap"
    one_safe = snap(positions=((1.1, 0.0), (1.08, 0.0)))
    assert b.verdict(KEY, one_safe, NOW).hold is None, "a stop moved to entry frees its place"


def test_a_sell_is_risk_free_on_the_other_side(tmp_path: Path) -> None:
    sells = tuple(
        Position(
            ticket=20 + i,
            symbol="EURUSD",
            type=PositionType.SELL,
            volume=1.0,
            price_open=1.1,
            sl=sl,
        )
        for i, sl in enumerate((1.1, 1.05, 1.15))
    )
    held = replace(snap(), open_positions=sells)
    status = only(book_with(open_rule(risk_free="ignored"), tmp=tmp_path), held)
    assert status.used == 1.0, "a sell's stop protects at or below its entry, never above"


def test_unread_positions_or_orders_cannot_be_measured(tmp_path: Path) -> None:
    status = only(book_with(open_rule(), tmp=tmp_path), replace(snap(), open_positions=None))
    assert status.level is Level.UNKNOWN and "open positions" in status.detail
    status = only(book_with(open_rule(pendings="counts"), tmp=tmp_path / "b"), snap())
    assert status.level is Level.UNKNOWN and "resting orders" in status.detail
