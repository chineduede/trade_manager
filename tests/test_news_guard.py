"""The news rule acting on open copies before a release: breakeven, close, cancel.

The one place a guard overrides the master, so the part that has to be right is that a copy it
closed stays closed while the master's trade runs on, and that it acts once, before the news.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_breakeven import (
    BUY,
    TID_SLAVE,
    _slave_at,
    _tick,
    copy_of,
    master_pos,
    slave,
)
from test_breakeven import live as live  # noqa: F401  (fixture)
from test_guards import KEY, NOW, snap
from test_news import FixedCalendar, calendar, feed_item, params

from mt5copier.breakeven import guard_stop
from mt5copier.engine import Engine
from mt5copier.guards import REGISTRY, AccountGuards, GuardBook, GuardRule, GuardsDoc, Verdict
from mt5copier.guards.book import NewsAct
from mt5copier.guards.rules.news import acting
from mt5copier.protocol import ActionKind, OrderResult, OrderType
from mt5copier.reconcile import CopiedOrder, CopyLedger, SlaveLink, reconcile_slave_orders
from mt5copier.store import Copy, CopyKind, MappingStore

NEWS = REGISTRY["news"]

# --- the rule ------------------------------------------------------------------------------------


def test_it_acts_from_its_own_minutes_before_until_the_release_never_after() -> None:
    cal = calendar(feed_item(10, "USD"))
    p = params(before_news="close", act_before=5)
    assert acting(p, cal, NOW)[0] == frozenset(), "10 min before: not yet"
    due, why = acting(p, cal, NOW + 5 * 60)
    assert due == {"USD"} and "USD CPI m/m" in why
    assert acting(p, cal, NOW + 10 * 60 - 1)[0] == {"USD"}
    assert acting(p, cal, NOW + 10 * 60)[0] == frozenset(), "at the release: too late"


def test_it_does_nothing_unless_asked_and_never_on_an_unread_calendar() -> None:
    cal = calendar(feed_item(1, "USD"))
    assert acting(params(), cal, NOW)[0] == frozenset(), "the default leaves copies alone"
    from mt5copier.news import Calendar

    blind = Calendar(None, "the news calendar could not be read")
    assert acting(params(before_news="close"), blind, NOW)[0] == frozenset()
    assert acting(params(before_news="close", affects="all"), cal, NOW)[0] == {"*"}


def test_the_sentence_says_what_happens_to_open_copies() -> None:
    said = NEWS.sentence(params(before_news="breakeven", act_before=3), "USD")
    assert "3 min before it, the copies in its currency are moved to breakeven" in said
    assert "closed and resting copies cancelled" in NEWS.sentence(
        params(before_news="close"), "USD"
    )
    assert "breakeven" not in NEWS.sentence(params(), "USD")


def _book(tmp: Path, **given: Any) -> GuardBook:
    rule = GuardRule(id="nw", type="news", params=given)
    doc = GuardsDoc(accounts={KEY: AccountGuards(rules=(rule,))})
    return GuardBook(
        doc, state_path=tmp / "state.json", calendar=FixedCalendar(calendar(feed_item(3, "USD")))
    )


def test_the_verdict_carries_what_to_do_and_warn_only_warns(tmp_path: Path) -> None:
    v = _book(tmp_path, before_news="breakeven").verdict(KEY, snap(), NOW)
    (act,) = v.news_acts
    assert (act.rule_id, act.mode, act.currencies) == ("nw", "breakeven", {"USD"})
    warned = _book(tmp_path, before_news="close", on_breach="warn").verdict(KEY, snap(), NOW)
    assert warned.news_acts == ()


# --- where the stop goes -------------------------------------------------------------------------


COSTS = slave(commission_per_lot_round_turn=7.0, slippage_buffer_points=3)


def test_a_copy_in_profit_gets_a_breakeven_stop_and_one_in_loss_none() -> None:
    copy = copy_of()
    assert guard_stop(copy, 1.08500, 1.08530, COSTS) == pytest.approx(1.08510)
    assert guard_stop(copy, 1.08500, 1.08505, COSTS) is None, "not clear of its costs"
    assert guard_stop(copy, 1.08500, 1.08400, COSTS) is None, "in loss"
    assert guard_stop(copy, 1.08500, None, COSTS) is None, "price unknown"
    assert guard_stop(copy_of(point=0.0), 1.08500, 1.08530, COSTS) is None
    assert guard_stop(copy_of(price_value=0.0), 1.08500, 1.08530, COSTS) is None


# --- the diff ------------------------------------------------------------------------------------


def test_a_flagged_copy_is_closed_while_the_master_runs_on_and_ignores_it() -> None:
    from mt5copier.reconcile import reconcile_slave

    flagged = copy_of(guard_close=True)
    moved = master_pos(sl=1.08100)
    actions = reconcile_slave([moved], [flagged], slave())
    assert [a.kind for a in actions] == [ActionKind.CLOSE]


def test_a_flagged_resting_copy_is_cancelled_even_if_the_master_fills() -> None:
    from mt5copier.protocol import PendingOrder

    order = PendingOrder(
        ticket=21, symbol="EURUSD", type=OrderType.BUY_LIMIT, volume=1.0, price_open=1.08
    )
    copy = CopiedOrder(
        terminal_id=TID_SLAVE,
        master_ticket=21,
        slave_symbol="EURUSD",
        order_type=OrderType.BUY_LIMIT,
        master_volume=1.0,
        entry=1.08,
        slave_ticket=950,
        guard_close=True,
    )
    resting = reconcile_slave_orders([order], [], [copy], slave())
    assert [a.kind for a in resting] == [ActionKind.CANCEL]
    filled = reconcile_slave_orders([], [21], [copy], slave())
    assert [a.kind for a in filled] == [ActionKind.CANCEL]


def test_once_closed_it_is_never_copied_again() -> None:
    ledger = CopyLedger()
    link = SlaveLink(slave(risk_percent=1.0), connected=True)
    ledger.step([], [link], now=0.0)  # the master starts flat
    (opened,) = ledger.step([master_pos()], [link], now=1.0)  # a dry run fills it at once
    assert opened.kind is ActionKind.OPEN and ledger.get_position(TID_SLAVE, 11) is not None
    assert ledger.request_close(TID_SLAVE, 11)
    (close,) = ledger.step([master_pos()], [link], now=2.0)
    assert close.kind is ActionKind.CLOSE
    assert ledger.get_position(TID_SLAVE, 11) is None
    assert ledger.step([master_pos()], [link], now=3.0) == []
    assert not ledger.request_close(TID_SLAVE, 11), "nothing left to close"


def test_the_flag_is_kept_on_disk(tmp_path: Path) -> None:
    with MappingStore(tmp_path / "m.db") as store:
        store.record(
            Copy(
                terminal_id=TID_SLAVE,
                master_ticket=11,
                kind=CopyKind.POSITION,
                slave_symbol="EURUSD",
                direction=BUY,
                master_volume=1.0,
                guard_close=True,
            )
        )
    with MappingStore(tmp_path / "m.db") as store:
        (copy,) = store.positions(TID_SLAVE)
        assert copy.guard_close


# --- the engine ----------------------------------------------------------------------------------


def _news(eng: Engine, mode: str) -> None:
    act = NewsAct("nw", "High-impact news", mode, frozenset({"USD"}), "USD CPI m/m at 14:30")
    eng._verdicts[TID_SLAVE] = Verdict(news_acts=(act,))


def _ok(eng: Engine, cmd: Any) -> None:
    eng._on_result(
        OrderResult(
            command_id=cmd.command_id,
            terminal_id=TID_SLAVE,
            op=cmd.op,
            master_ticket=11,
            ok=True,
            slave_ticket=900,
        )
    )


def _said(eng: Engine) -> list[str]:
    return [g.text() for e in eng.day_book() for o in e.outcomes for g in o.guard]


def test_close_mode_closes_the_copy_once_and_says_so(live: Engine) -> None:  # noqa: F811
    _slave_at(live, 1.08450)
    _news(live, "close")
    (cmd,) = _tick(live)
    assert cmd.op is ActionKind.CLOSE
    assert any("High-impact news closed it: before USD CPI" in s for s in _said(live))
    from mt5copier.day_book import describe_master

    (event,) = live.day_book()
    assert describe_master(event) == (
        "High-impact news closed a slave's copy of EURUSD buy 1; the master's is unchanged"
    )
    _ok(live, cmd)
    assert _tick(live) == [], "not reopened while the master's trade runs on"
    assert _tick(live) == []


def test_breakeven_mode_moves_a_copy_in_profit(live: Engine) -> None:  # noqa: F811
    _slave_at(live, 1.08530)
    _news(live, "breakeven")
    (cmd,) = _tick(live)
    assert cmd.op is ActionKind.MODIFY_SLTP and cmd.sl == pytest.approx(1.08510)
    assert any("moved it: its stop to breakeven, 1.0851" in s for s in _said(live))
    _ok(live, cmd)
    assert _tick(live) == [], "acted on once in the window"


def test_breakeven_mode_closes_a_copy_in_loss(live: Engine) -> None:  # noqa: F811
    _slave_at(live, 1.08400)
    _news(live, "breakeven")
    (cmd,) = _tick(live)
    assert cmd.op is ActionKind.CLOSE
    assert any("breakeven stop was impossible" in s for s in _said(live))


def test_a_refused_guard_breakeven_closes_the_copy(live: Engine) -> None:  # noqa: F811
    _slave_at(live, 1.08530)
    _news(live, "breakeven")
    (cmd,) = _tick(live)
    live._on_result(
        OrderResult(
            command_id=cmd.command_id,
            terminal_id=TID_SLAVE,
            op=cmd.op,
            master_ticket=11,
            ok=False,
            retryable=False,
            error="invalid stops",
        )
    )
    (close,) = _tick(live)
    assert close.op is ActionKind.CLOSE


def test_a_copy_in_another_currency_is_left_alone(live: Engine) -> None:  # noqa: F811
    _slave_at(live, 1.08450)
    act = NewsAct("nw", "High-impact news", "close", frozenset({"JPY"}), "JPY BoJ at 03:00")
    live._verdicts[TID_SLAVE] = Verdict(news_acts=(act,))
    assert _tick(live) == []
