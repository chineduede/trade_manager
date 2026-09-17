"""Executor tests, driven by a fake session with scripted retcodes.

This is the only module that can cause an order to exist, so the tests are weighted toward
the refusals: the live-account guard, an unresolvable symbol, a size under the minimum. A
copier that declines to trade is recoverable; one that trades the wrong thing is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from mt5copier.execution import OrderFilling, Retcode, SymbolFilling
from mt5copier.executor import Executor
from mt5copier.protocol import ActionKind, OrderCommand, OrderType, PositionType, SizingBasis
from mt5copier.sizing import SymbolFacts

TID = "B" * 32


@dataclass
class FakeResult:
    """Shaped like MT5's OrderSendResult."""

    retcode: int = int(Retcode.DONE)
    order: int = 555_001
    volume: float = 0.0
    price: float = 1.08510


@dataclass
class FakeSession:
    trade_mode: int | None = 0  # demo
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD")
    facts: SymbolFacts | None = None
    loss: float | None = 500.0
    quote: tuple[float, float] | None = (1.08500, 1.08510)
    results: list[Any] = field(default_factory=list)
    """Scripted, one per attempt. The last one repeats once exhausted."""
    sent: list[dict[str, Any]] = field(default_factory=list)

    def account_trade_mode(self) -> int | None:
        return self.trade_mode

    def symbol_names(self) -> list[str]:
        return list(self.symbols)

    def symbol_facts(self, name: str) -> SymbolFacts | None:
        if self.facts is not None:
            return self.facts
        return SymbolFacts(
            name=name,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            point=0.00001,
            filling_mask=int(SymbolFilling.FOK | SymbolFilling.IOC),
        )

    def tick(self, name: str) -> tuple[float, float] | None:
        return self.quote

    def loss_per_lot(
        self, direction: PositionType, symbol: str, entry: float, sl: float
    ) -> float | None:
        return self.loss

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sent.append(dict(request))
        if not self.results:
            return FakeResult()
        index = min(len(self.sent) - 1, len(self.results) - 1)
        return self.results[index]


def cmd(**kw: object) -> OrderCommand:
    base: dict[str, Any] = {
        "command_id": 1,
        "op": ActionKind.OPEN,
        "master_ticket": 42,
        "master_symbol": "EURUSD",
        "direction": PositionType.BUY,
        "entry": 1.08500,
        "sl": 1.08000,
        "master_volume": 1.0,
        "risk_amount": 1_000.0,
    }
    base.update(kw)
    return OrderCommand(**base)


def run(session: FakeSession, command: OrderCommand | None = None):
    return Executor(TID, session).execute(command or cmd())


# --- the guard ------------------------------------------------------------------------------


def test_a_demo_account_is_allowed() -> None:
    assert run(FakeSession(trade_mode=0)).ok


def test_a_real_account_is_refused_and_nothing_is_sent() -> None:
    session = FakeSession(trade_mode=2)
    result = run(session)
    assert not result.ok
    assert "not a demo" in (result.error or "")
    assert session.sent == [], "the guard must run before anything is sent"


def test_a_contest_account_is_refused_too() -> None:
    assert not run(FakeSession(trade_mode=1)).ok


def test_an_unreadable_trade_mode_counts_as_not_demo() -> None:
    """ "We could not tell" is not a reason to trade someone's funded account."""
    session = FakeSession(trade_mode=None)
    assert not run(session).ok
    assert session.sent == []


def test_the_override_is_explicit_and_per_command() -> None:
    session = FakeSession(trade_mode=2)
    assert run(session, cmd(allow_live=True)).ok
    assert len(session.sent) == 1


# --- refusals before sending ----------------------------------------------------------------


def test_an_unresolvable_symbol_is_refused() -> None:
    session = FakeSession(symbols=("USDJPY",))
    result = run(session)
    assert not result.ok
    assert "not found" in (result.error or "")
    assert session.sent == []


def test_a_size_below_the_minimum_sends_nothing() -> None:
    """Rounding up to volume_min could be several times the intended risk."""
    session = FakeSession(loss=1_000_000.0)
    result = run(session)
    assert not result.ok
    assert "minimum" in (result.error or "")
    assert session.sent == []


def test_an_unpriceable_move_is_refused_rather_than_guessed() -> None:
    session = FakeSession(loss=None)
    assert not run(session).ok
    assert session.sent == []


def test_operations_that_need_the_mapping_table_are_declined_not_faked() -> None:
    for op in (ActionKind.CLOSE, ActionKind.REDUCE, ActionKind.CANCEL):
        session = FakeSession()
        result = run(session, cmd(op=op))
        assert not result.ok
        assert session.sent == []


# --- the request ----------------------------------------------------------------------------


def test_the_request_carries_the_computed_size_not_the_masters() -> None:
    session = FakeSession()
    run(session, cmd(master_volume=7.0, risk_amount=1_000.0))  # loss 500/lot -> 2 lots
    assert session.sent[0]["volume"] == pytest.approx(2.0)


def test_a_buy_is_priced_at_the_ask_and_a_sell_at_the_bid() -> None:
    session = FakeSession()
    run(session, cmd(direction=PositionType.BUY))
    assert session.sent[0]["price"] == pytest.approx(1.08510)

    session = FakeSession()
    run(session, cmd(direction=PositionType.SELL, entry=1.08, sl=1.09))
    assert session.sent[0]["price"] == pytest.approx(1.08500)


def test_stops_are_only_included_when_set() -> None:
    session = FakeSession()
    run(
        session, cmd(sl=0.0, tp=0.0, sizing_basis=SizingBasis.MASTER_LOT_MULTIPLE, lot_multiple=1.0)
    )
    assert "sl" not in session.sent[0]
    assert "tp" not in session.sent[0]


def test_a_pending_order_is_placed_at_its_own_price_not_the_market() -> None:
    session = FakeSession()
    run(session, cmd(op=ActionKind.PLACE, order_type=OrderType.BUY_LIMIT, entry=1.07000))
    request = session.sent[0]
    assert request["action"] == 5, "TRADE_ACTION_PENDING"
    assert request["type"] == int(OrderType.BUY_LIMIT)
    assert request["price"] == pytest.approx(1.07000)
    assert request["type_filling"] == int(OrderFilling.RETURN)


def test_a_market_order_uses_a_filling_mode_the_symbol_permits() -> None:
    session = FakeSession()
    session.facts = SymbolFacts(
        name="EURUSD",
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        point=0.00001,
        filling_mask=int(SymbolFilling.IOC),
    )
    run(session)
    assert session.sent[0]["type_filling"] == int(OrderFilling.IOC)


def test_the_configured_deviation_is_passed_through() -> None:
    session = FakeSession()
    run(session, cmd(deviation_points=7))
    assert session.sent[0]["deviation"] == 7


# --- retries --------------------------------------------------------------------------------


def test_a_requote_is_repriced_and_retried() -> None:
    session = FakeSession(
        results=[FakeResult(retcode=int(Retcode.REQUOTE)), FakeResult(retcode=int(Retcode.DONE))]
    )
    result = run(session)
    assert result.ok
    assert result.attempts == 2
    assert len(session.sent) == 2


def test_invalid_fill_retries_with_the_other_mode() -> None:
    """The failure zele-mt5 cannot recover from, because it never reads the mask."""
    session = FakeSession(
        results=[
            FakeResult(retcode=int(Retcode.INVALID_FILL)),
            FakeResult(retcode=int(Retcode.DONE)),
        ]
    )
    result = run(session)
    assert result.ok
    assert session.sent[0]["type_filling"] == int(OrderFilling.FOK)
    assert session.sent[1]["type_filling"] == int(OrderFilling.IOC)


def test_invalid_fill_gives_up_when_the_mask_permits_no_alternative() -> None:
    session = FakeSession(results=[FakeResult(retcode=int(Retcode.INVALID_FILL))])
    session.facts = SymbolFacts(
        name="EURUSD",
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        point=0.00001,
        filling_mask=int(SymbolFilling.FOK),
    )
    result = run(session)
    assert not result.ok
    assert len(session.sent) == 1, "no point retrying a mode the symbol also forbids"


def test_a_fatal_retcode_is_not_retried() -> None:
    session = FakeSession(results=[FakeResult(retcode=int(Retcode.NO_MONEY))])
    result = run(session)
    assert not result.ok
    assert len(session.sent) == 1
    assert "NO_MONEY" in (result.error or "")


def test_retries_are_bounded() -> None:
    """A copy still retrying after seconds is no longer the trade the master took."""
    session = FakeSession(results=[FakeResult(retcode=int(Retcode.REQUOTE))])
    result = run(session)
    assert not result.ok
    assert len(session.sent) == 3
    assert result.attempts == 3


def test_a_none_result_stops_rather_than_hammering() -> None:
    session = FakeSession(results=[None])
    result = run(session)
    assert not result.ok
    assert len(session.sent) == 1


# --- results --------------------------------------------------------------------------------


def test_a_fill_reports_the_slave_ticket_and_symbol() -> None:
    session = FakeSession(results=[FakeResult(order=999, volume=2.0)])
    result = run(session)
    assert result.ok
    assert result.slave_ticket == 999
    assert result.slave_symbol == "EURUSD"
    assert result.filled_volume == pytest.approx(2.0)


def test_a_partial_fill_records_what_was_filled_not_what_was_asked() -> None:
    """Recording the request would leave the copier believing in volume that does not exist,
    and every later partial close would be computed against a fiction."""
    session = FakeSession(results=[FakeResult(retcode=int(Retcode.DONE_PARTIAL), volume=0.75)])
    result = run(session)
    assert result.ok
    assert result.requested_volume == pytest.approx(2.0)
    assert result.filled_volume == pytest.approx(0.75)


def test_a_crash_in_the_executor_becomes_a_failed_result_not_an_exception() -> None:
    class Exploding(FakeSession):
        def symbol_names(self) -> list[str]:
            raise RuntimeError("boom")

    result = run(Exploding())
    assert not result.ok
    assert "crashed" in (result.error or "")


def test_the_symbol_resolution_is_cached_across_commands() -> None:
    session = FakeSession(symbols=("EURUSD.r",))
    executor = Executor(TID, session)
    assert executor.execute(cmd()).slave_symbol == "EURUSD.r"

    session.symbols = ()
    assert executor.execute(cmd(command_id=2)).slave_symbol == "EURUSD.r"
