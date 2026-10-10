"""The master's own breakeven: the one thing that makes the copier trade the master. When a
master trade is far enough in profit its worker moves its stop past entry by the trade's own
commission, once and never looser, and books a share of it if asked. Built on the breakeven
tests' live engine: the master long 1 lot of EURUSD at 1.08500, its stop 50 pips away."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_breakeven import _Q, TID_MASTER, TID_SLAVE, _slave_at, _Worker, live, master_pos
from test_executor import FakeSession, cmd, run

from mt5copier.breakeven import master_breakeven_due, stop_past_entry
from mt5copier.config import BreakevenMode
from mt5copier.engine import Engine
from mt5copier.protocol import (
    ActionKind,
    OrderCommand,
    OrderResult,
    PositionLive,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.store import MappingStore, MasterTrade

__all__ = ["live"]  # the fixture, imported for the tests below

BUY, SELL = PositionType.BUY, PositionType.SELL


# --- when, and where to --------------------------------------------------------------------


def test_a_master_trade_is_due_at_its_trigger_in_r() -> None:
    assert not master_breakeven_due(BUY, 1.08500, 1.08590, 0.005, 0.2)
    assert master_breakeven_due(BUY, 1.08500, 1.08600, 0.005, 0.2)
    assert master_breakeven_due(SELL, 1.08500, 1.08400, 0.005, 0.2)
    assert not master_breakeven_due(SELL, 1.08500, 1.08600, 0.005, 0.2)
    assert not master_breakeven_due(BUY, 1.08500, 1.09000, 0.0, 0.2), "no stop, no R"


def test_the_stop_is_rounded_away_from_entry() -> None:
    assert stop_past_entry(BUY, 1.08500, 0.000281, 0.00001, 5) == pytest.approx(1.08529)
    assert stop_past_entry(SELL, 1.08500, 0.000281, 0.00001, 5) == pytest.approx(1.08471)


# --- the master's worker -------------------------------------------------------------------


def _breakeven(session: FakeSession, **kw: Any) -> OrderResult:
    base: dict[str, Any] = {
        "op": ActionKind.MODIFY_SLTP,
        "to_breakeven": True,
        "slave_ticket": 7,
        "entry": 1.08500,
        "sl": 1.08000,
        "tp": 1.09500,
        "slippage_points": 3,
    }
    return run(session, cmd(**(base | kw)))


def _master_session(**kw: Any) -> FakeSession:
    session = FakeSession(**kw)
    session.positions[7] = ("EURUSD", BUY, 0.5)
    # FakeSession prices any move at 500 a lot over the thousand points it is asked about:
    # 50,000 for a 1.0 move.
    session.quote = (1.08600, 1.08610)
    return session


def test_the_master_stop_goes_past_entry_by_its_own_commission_doubled() -> None:
    session = _master_session()
    session.position_paid = (-3.5, 0.5)  # type: ignore[attr-defined]
    result = _breakeven(session)
    assert result.ok, result.error
    # $3.50 in on 0.5 lots: $14 a lot there and back, 0.00028 of price, plus 3 points.
    assert result.price == pytest.approx(1.08531)
    (request,) = session.sent
    assert request["position"] == 7
    assert (request["sl"], request["tp"]) == (pytest.approx(1.08531), 1.09500)


def test_without_a_commission_on_the_deal_the_setting_stands_in() -> None:
    session = _master_session()
    result = _breakeven(session, commission_per_lot=7.0, slippage_points=0)
    assert result.ok, result.error
    assert result.price == pytest.approx(1.08514)


def test_a_master_stop_already_past_breakeven_is_left_alone() -> None:
    session = _master_session()
    result = _breakeven(session, sl=1.08600)
    assert result.ok and result.outcome == "already past breakeven"
    assert session.sent == [], "never loosened"


def test_until_the_price_is_past_the_level_it_is_asked_again_later() -> None:
    session = _master_session()
    session.quote = (1.08502, 1.08512)
    result = _breakeven(session)
    assert not result.ok and result.retryable
    assert "not past the breakeven stop" in (result.error or "")
    assert session.sent == []


def test_a_master_position_already_closed_is_done() -> None:
    session = FakeSession()
    result = _breakeven(session)
    assert result.ok and result.target_gone and session.sent == []


def test_the_live_guard_covers_the_master_too() -> None:
    session = _master_session(trade_mode=2)
    result = _breakeven(session)
    assert not result.ok and "not a demo" in (result.error or "")
    assert session.sent == []


# --- what the store keeps ------------------------------------------------------------------


def test_the_master_trades_round_trip_and_closed_ones_are_dropped(tmp_path: Path) -> None:
    with MappingStore(tmp_path / "m.db") as store:
        store.remember_master_trade(MasterTrade(TID_MASTER, 1, 0.005))
        store.remember_master_trade(MasterTrade(TID_MASTER, 2, 0.004, breakeven_done=True))
    with MappingStore(tmp_path / "m.db") as store:
        assert store.master_trades(TID_MASTER)[2].breakeven_done
        store.forget_master_trades(TID_MASTER, keep=[2])
        assert list(store.master_trades(TID_MASTER)) == [2]
        store.forget_master_trades(TID_MASTER, keep=[])
        assert store.master_trades(TID_MASTER) == {}


# --- the engine ----------------------------------------------------------------------------


@pytest.fixture
def master_be(live: Engine) -> Engine:
    """The live engine, its master set to breakeven at 0.2R and book half, with a worker."""
    master = live.config.terminals[TID_MASTER]
    live.config.replace_terminal(
        master.model_copy(
            update={
                "breakeven_mode": BreakevenMode.R,
                "breakeven_trigger_r": 0.2,
                "breakeven_book_percent": 50.0,
            }
        )
    )
    worker = _Worker()
    worker.terminal_id = TID_MASTER
    live._workers[TID_MASTER] = worker  # type: ignore[assignment]
    return live


def _master_tick(eng: Engine, price: float, *, sl: float = 1.08000) -> list[OrderCommand]:
    eng._reconcile(
        Snapshot(
            terminal_id=TID_MASTER,
            state=WorkerState.CONNECTED,
            open_positions=(replace(master_pos(), sl=sl),),
            live_positions=(PositionLive(ticket=11, price_current=price),),
            pending_orders=(),
        )
    )
    q = eng._workers[TID_MASTER].cmd_q
    assert isinstance(q, _Q)
    sent = [c for c in q.sent if isinstance(c, OrderCommand)]
    q.sent.clear()
    return sent


def _answer(eng: Engine, sent: OrderCommand, **kw: Any) -> None:
    base: dict[str, Any] = {"ok": True, "price": 1.08531}
    eng._on_result(
        OrderResult(
            command_id=sent.command_id,
            terminal_id=TID_MASTER,
            op=sent.op,
            master_ticket=sent.master_ticket,
            **(base | kw),
        )
    )


def test_the_master_goes_to_breakeven_once_then_books_once(master_be: Engine) -> None:
    assert _master_tick(master_be, 1.08590) == [], "short of 0.2R"
    (be,) = _master_tick(master_be, 1.08610)
    assert be.op is ActionKind.MODIFY_SLTP and be.to_breakeven
    assert (be.slave_ticket, be.entry, be.sl) == (11, 1.08500, 1.08000)
    assert _master_tick(master_be, 1.08620) == [], "one at a time"
    _answer(master_be, be)

    (book,) = _master_tick(master_be, 1.08620, sl=1.08531)
    assert book.op is ActionKind.BOOK and book.close_fraction == pytest.approx(0.5)
    assert book.slave_ticket == 11
    _answer(master_be, book, filled_volume=0.5)
    assert _master_tick(master_be, 1.08700, sl=1.08531) == []
    # The person loosening the stop afterwards is left alone.
    assert _master_tick(master_be, 1.08700, sl=1.08000) == []


def test_r_is_measured_from_the_stop_the_trade_was_first_seen_with(master_be: Engine) -> None:
    _master_tick(master_be, 1.08550)
    # The stop trailed up to 1.08300: R stays the first 50 pips, so 0.2R is still 10 pips.
    assert _master_tick(master_be, 1.08590, sl=1.08300) == []
    (be,) = _master_tick(master_be, 1.08610, sl=1.08300)
    assert be.to_breakeven


def test_a_breakeven_not_set_yet_is_asked_again_after_a_wait(
    master_be: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    (be,) = _master_tick(master_be, 1.08610)
    _answer(master_be, be, ok=False, retryable=True, error="not past it yet", price=None)
    assert _master_tick(master_be, 1.08620) == []
    later = time.monotonic() + 16
    monkeypatch.setattr(time, "monotonic", lambda: later)
    (again,) = _master_tick(master_be, 1.08620)
    assert again.to_breakeven


def test_a_breakeven_done_survives_a_restart(master_be: Engine, tmp_path: Path) -> None:
    (be,) = _master_tick(master_be, 1.08610)
    _answer(master_be, be)
    again = Engine(
        master_be.config,
        state_path=tmp_path / "dryrun.db",
        mapping_path=tmp_path / "mapping.db",
    )
    worker = _Worker()
    worker.terminal_id = TID_MASTER
    again._workers[TID_MASTER] = worker  # type: ignore[assignment]
    sent = _master_tick(again, 1.08700, sl=1.08000)
    assert [c.op for c in sent] == [ActionKind.BOOK], "no second breakeven; the booking owed"


def test_nothing_is_sent_to_the_master_with_its_breakeven_off(live: Engine) -> None:
    worker = _Worker()
    worker.terminal_id = TID_MASTER
    live._workers[TID_MASTER] = worker  # type: ignore[assignment]
    assert _master_tick(live, 1.09000) == []


def test_the_master_is_not_traded_in_a_dry_run(master_be: Engine) -> None:
    master_be.config.dry_run = True
    assert _master_tick(master_be, 1.08610) == []


def test_a_copy_the_master_manages_follows_its_breakeven(master_be: Engine) -> None:
    """The master's breakeven is a stop move like any other: a copy the master manages
    follows it, and its slave's own breakeven stays out of it."""
    master_be._ensure_store().update(TID_SLAVE, 11, master_managed=True)
    _slave_at(master_be, 1.08620)
    (be,) = _master_tick(master_be, 1.08610)
    _answer(master_be, be)
    _master_tick(master_be, 1.08620, sl=1.08531)
    slave_q = master_be._workers[TID_SLAVE].cmd_q
    assert isinstance(slave_q, _Q)
    (moved,) = [c for c in slave_q.sent if isinstance(c, OrderCommand)]
    assert moved.op is ActionKind.MODIFY_SLTP and moved.sl == pytest.approx(1.08531)
