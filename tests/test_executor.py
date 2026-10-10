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
from mt5copier.executor import UNREADABLE, Executor, ReadFailure
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
    deal: int = 777_001


@dataclass
class FakeSession:
    trade_mode: int | None = 0  # demo
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD")
    facts: SymbolFacts | None = None
    loss: float | None = 500.0
    quote: tuple[float, float] | None = (1.08500, 1.08510)
    quote_sequence: list[tuple[float, float] | None] | None = None
    """Scripted per-call ticks, for the just-selected-symbol retry; falls back to `quote`."""
    tick_calls: list[str] = field(default_factory=list)
    results: list[Any] = field(default_factory=list)
    """Scripted, one per attempt. The last one repeats once exhausted."""
    sent: list[dict[str, Any]] = field(default_factory=list)
    positions: dict[int, tuple[str, PositionType, float]] = field(default_factory=dict)
    resting_orders: set[int] = field(default_factory=set)
    position_unreadable: bool = False
    """The terminal refusing the read, which is not the same as the position being gone."""
    commissions: dict[int, float] = field(default_factory=dict)
    """Each deal's ``commission`` as MT5 records it, negative for a charge."""
    deals_read: list[int] = field(default_factory=list)

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
        self.tick_calls.append(name)
        if self.quote_sequence is not None:
            index = min(len(self.tick_calls) - 1, len(self.quote_sequence) - 1)
            return self.quote_sequence[index]
        return self.quote

    def position(self, ticket: int) -> tuple[str, PositionType, float] | None | ReadFailure:
        if self.position_unreadable:
            return UNREADABLE
        return self.positions.get(ticket)

    def order_exists(self, ticket: int) -> bool:
        return ticket in self.resting_orders

    def loss_per_lot(
        self, direction: PositionType, symbol: str, entry: float, sl: float
    ) -> float | None:
        return self.loss

    def deal_commission(self, deal: int) -> float | None:
        self.deals_read.append(deal)
        if deal < 0:
            raise RuntimeError("the terminal fell over")
        return self.commissions.get(deal)

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


def test_an_operation_with_no_recorded_slave_ticket_is_declined() -> None:
    """Without the mapping there is nothing to act on, and guessing is not an option."""
    for op in (
        ActionKind.CLOSE,
        ActionKind.REDUCE,
        ActionKind.CANCEL,
        ActionKind.MODIFY_SLTP,
        ActionKind.MODIFY_ORDER,
    ):
        session = FakeSession()
        result = run(session, cmd(op=op, slave_ticket=None))
        assert not result.ok
        assert session.sent == []


# --- the first tick on a freshly selected symbol ---------------------------------------------
#
# symbol_facts() can succeed -- the symbol is selected and visible -- while symbol_info_tick()
# still has nothing for a stretch afterward. A failed OPEN is final, so one unlucky read here
# is not "retried later", it is the trade gone; one retry after a short wait covers the gap.


def test_a_permanently_missing_tick_is_refused_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("mt5copier.executor.time.sleep", sleeps.append)
    session = FakeSession(quote=None)
    result = run(session)
    assert not result.ok
    assert "no tick" in (result.error or "")
    assert session.sent == [], "must never reach order_send without a price"
    assert sleeps == [2.0], "exactly one retry, not a loop"
    assert session.tick_calls == ["EURUSD", "EURUSD"]


def test_a_tick_that_arrives_on_the_retry_still_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact gap this was written for: selected and visible, but no price on the very first
    read -- recovering a moment later must not already have given up."""
    sleeps: list[float] = []
    monkeypatch.setattr("mt5copier.executor.time.sleep", sleeps.append)
    session = FakeSession(quote_sequence=[None, (1.08500, 1.08510)])
    result = run(session)
    assert result.ok
    assert sleeps == [2.0]
    assert session.sent and session.sent[0]["price"] == 1.08510


def test_an_immediately_available_tick_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common path must not pay for a gap it does not have."""
    sleeps: list[float] = []
    monkeypatch.setattr("mt5copier.executor.time.sleep", sleeps.append)
    assert run(FakeSession()).ok
    assert sleeps == []


# --- closing --------------------------------------------------------------------------------


def closing_session(volume: float = 2.0, direction: PositionType = PositionType.BUY) -> FakeSession:
    session = FakeSession()
    session.positions[77] = ("EURUSD", direction, volume)
    return session


def test_closing_sends_the_opposite_side_for_the_whole_position() -> None:
    session = closing_session(volume=2.0, direction=PositionType.BUY)
    result = run(session, cmd(op=ActionKind.CLOSE, slave_ticket=77))
    assert result.ok
    request = session.sent[0]
    assert request["position"] == 77
    assert request["type"] == 1, "a long is closed by selling"
    assert request["volume"] == pytest.approx(2.0)
    assert request["price"] == pytest.approx(1.08500), "sell at the bid"


def test_closing_a_short_buys_at_the_ask() -> None:
    session = closing_session(direction=PositionType.SELL)
    run(session, cmd(op=ActionKind.CLOSE, slave_ticket=77))
    assert session.sent[0]["type"] == 0
    assert session.sent[0]["price"] == pytest.approx(1.08510)


def test_closing_uses_the_terminals_volume_not_the_recorded_one() -> None:
    """Ours can be stale -- the user may have closed part of it by hand -- and a stale figure
    either gets rejected or closes the wrong amount."""
    session = closing_session(volume=0.30)
    run(session, cmd(op=ActionKind.CLOSE, slave_ticket=77, master_volume=5.0))
    assert session.sent[0]["volume"] == pytest.approx(0.30)


def test_closing_something_already_gone_is_a_success() -> None:
    """The desired state is "not open", and it is not open. Reporting failure would leave the
    engine retrying a close against a position that no longer exists."""
    session = FakeSession()
    result = run(session, cmd(op=ActionKind.CLOSE, slave_ticket=77))
    assert result.ok
    assert result.filled_volume == 0.0
    assert session.sent == []


# --- partial closes ---------------------------------------------------------------------------


def test_a_partial_closes_the_fraction_of_the_live_volume() -> None:
    session = closing_session(volume=1.0)
    run(session, cmd(op=ActionKind.REDUCE, slave_ticket=77, close_fraction=0.4))
    assert session.sent[0]["volume"] == pytest.approx(0.4)


def test_a_partial_is_quantized_to_the_symbols_step() -> None:
    session = closing_session(volume=1.0)
    session.facts = SymbolFacts(
        name="EURUSD",
        volume_min=0.05,
        volume_max=100.0,
        volume_step=0.05,
        point=0.00001,
        filling_mask=int(SymbolFilling.FOK | SymbolFilling.IOC),
    )
    run(session, cmd(op=ActionKind.REDUCE, slave_ticket=77, close_fraction=0.33))
    assert session.sent[0]["volume"] == pytest.approx(0.30)


def test_a_partial_too_small_to_express_closes_nothing() -> None:
    """Over-closing cannot be undone, so leaving the position whole is the safe answer."""
    session = closing_session(volume=0.02)
    result = run(session, cmd(op=ActionKind.REDUCE, slave_ticket=77, close_fraction=0.1))
    assert not result.ok
    assert session.sent == []


def test_a_partial_can_never_exceed_the_open_volume() -> None:
    session = closing_session(volume=0.50)
    run(session, cmd(op=ActionKind.REDUCE, slave_ticket=77, close_fraction=0.99))
    assert session.sent[0]["volume"] <= 0.50


def test_a_nonsensical_fraction_is_refused() -> None:
    session = closing_session()
    for fraction in (0.0, 1.0, 1.5, -0.2):
        assert not run(
            session, cmd(op=ActionKind.REDUCE, slave_ticket=77, close_fraction=fraction)
        ).ok
    assert session.sent == []


# --- cancelling -----------------------------------------------------------------------------


def test_cancelling_removes_the_resting_order() -> None:
    session = FakeSession(resting_orders={88})
    result = run(session, cmd(op=ActionKind.CANCEL, slave_ticket=88))
    assert result.ok
    assert session.sent[0]["action"] == 8, "TRADE_ACTION_REMOVE"
    assert session.sent[0]["order"] == 88


def test_cancelling_an_order_that_already_went_is_a_success() -> None:
    """It filled, or someone removed it. Either way it is not resting."""
    session = FakeSession(resting_orders=set())
    result = run(session, cmd(op=ActionKind.CANCEL, slave_ticket=88))
    assert result.ok
    assert session.sent == []


def test_a_rejected_cancel_is_reported() -> None:
    session = FakeSession(resting_orders={88}, results=[FakeResult(retcode=int(Retcode.INVALID))])
    assert not run(session, cmd(op=ActionKind.CANCEL, slave_ticket=88)).ok


# --- modifying a position's stop and target ---------------------------------------------------


def modify(**kw: object) -> OrderCommand:
    base: dict[str, Any] = {"op": ActionKind.MODIFY_SLTP, "slave_ticket": 77, "tp": 1.09500}
    base.update(kw)
    return cmd(**base)


def test_a_stop_move_is_sent_as_an_sltp_action_on_the_position() -> None:
    """SLTP changes a position's levels and nothing else -- no volume, no fill mode, no price."""
    session = closing_session()
    run(session, modify(sl=1.08250))
    request = session.sent[0]
    assert request["action"] == 6, "TRADE_ACTION_SLTP"
    assert request["position"] == 77
    assert request["sl"] == pytest.approx(1.08250)
    assert request["tp"] == pytest.approx(1.09500)
    for absent in ("volume", "type_filling", "price", "deviation", "type"):
        assert absent not in request, f"{absent} is not part of an SLTP request"


def test_an_sltp_request_takes_its_symbol_from_the_terminal_not_our_record() -> None:
    """Our record is the master's spelling put through a prefix/suffix; the position knows
    what it actually is. Same reason the close volume is read live."""
    session = FakeSession()
    session.positions[77] = ("EURUSD.r", PositionType.BUY, 1.0)
    run(session, modify(master_symbol="GBPUSD"))
    assert session.sent[0]["symbol"] == "EURUSD.r"


def test_clearing_a_stop_is_sent_as_zero_rather_than_omitted() -> None:
    """A master removing its stop is a real action to mirror. Under TRADE_ACTION_SLTP an
    omitted field and a zero are the same bytes, so this asserts intent rather than encoding
    -- the OPEN path's "only include a stop when set" idiom must not leak in here."""
    session = closing_session()
    run(session, modify(sl=0.0, tp=0.0))
    assert session.sent[0]["sl"] == 0.0
    assert session.sent[0]["tp"] == 0.0


def test_the_levels_are_floats_even_when_handed_whole_numbers() -> None:
    """order_send rejects an int where it wants a double and answers None -- which classify()
    would then report as a lost connection, for a request that never left the process."""
    session = closing_session()
    run(session, modify(sl=1, tp=2))
    assert isinstance(session.sent[0]["sl"], float)
    assert isinstance(session.sent[0]["tp"], float)


def test_modifying_a_position_that_is_gone_is_not_an_error() -> None:
    """There is nothing left to protect, so nothing left to get wrong. Reporting failure
    instead would mean one error per distinct stop value for as long as the master holds the
    trade -- the log rot DISPATCHABLE_KINDS was filtered to avoid."""
    session = FakeSession()
    result = run(session, modify())
    assert result.ok
    assert session.sent == []


def test_an_unreadable_position_is_not_treated_as_a_gone_one() -> None:
    """ "We could not tell" must not become "already synced": belief would record a stop the
    slave never got, and the diff never re-reads the slave."""
    for op, extra in (
        (ActionKind.MODIFY_SLTP, {}),
        (ActionKind.CLOSE, {}),
        (ActionKind.REDUCE, {"close_fraction": 0.5}),
    ):
        session = FakeSession(position_unreadable=True)
        result = run(session, cmd(op=op, slave_ticket=77, **extra))
        assert not result.ok, op
        assert result.retryable, "transient, so the next tick should derive it again"
        assert session.sent == []


def test_a_rejected_stop_move_names_the_levels_it_was_refused() -> None:
    """INVALID_STOPS -- the stop is inside the broker's minimum distance from the market -- is
    the routine refusal here, and "it was refused" without saying what is not actionable."""
    session = closing_session()
    session.results = [FakeResult(retcode=int(Retcode.INVALID_STOPS))]
    result = run(session, modify(sl=1.08250))
    assert not result.ok
    assert not result.retryable, "the broker refused it; sending it again changes nothing"
    assert "INVALID_STOPS" in (result.error or "")
    assert "1.0825" in (result.error or "")


def test_a_modification_is_never_repriced() -> None:
    """A requote asks for a fresh market price. These prices are the master's, and there is no
    price field in an SLTP request at all -- repricing would invent one."""
    session = closing_session()
    session.results = [FakeResult(retcode=int(Retcode.REQUOTE))]
    result = run(session, modify(sl=1.08250))
    assert not result.ok
    assert len(session.sent) == 1, "no reprice loop"


def test_a_connection_failure_on_a_modify_is_reported_as_transient() -> None:
    session = closing_session()
    session.results = [FakeResult(retcode=int(Retcode.CONNECTION))]
    result = run(session, modify(sl=1.08250))
    assert not result.ok
    assert result.retryable


def test_no_changes_counts_as_synced() -> None:
    """What makes a modification safe to retry: a repeat that changes nothing is accepted."""
    session = closing_session()
    session.results = [FakeResult(retcode=int(Retcode.NO_CHANGES))]
    result = run(session, modify(sl=1.08250))
    assert result.ok
    assert result.outcome == "already matched"


def test_the_server_saying_the_position_closed_is_not_a_failure() -> None:
    """The race the local read cannot close: it went between our read and our send."""
    session = closing_session()
    session.results = [FakeResult(retcode=int(Retcode.POSITION_CLOSED))]
    assert run(session, modify(sl=1.08250)).ok


def test_a_modify_result_reports_no_volume_and_no_new_ticket() -> None:
    """MT5 creates no order and no deal for a modification, so it answers with zeroes.
    Reporting those as a fill would read like a trade that went nowhere."""
    session = closing_session()
    result = run(session, modify(sl=1.08250))
    assert result.ok
    assert "filled" not in result.describe()
    assert result.describe().startswith("MODIFY_SLTP  #42 EURUSD")


# --- modifying a resting order -----------------------------------------------------------------


def test_moving_a_resting_order_is_sent_as_a_modify_action() -> None:
    """The one thing a position cannot do: a resting order's entry price is a parameter."""
    session = FakeSession(resting_orders={88})
    run(
        session,
        cmd(
            op=ActionKind.MODIFY_ORDER,
            order_type=OrderType.BUY_LIMIT,
            slave_ticket=88,
            entry=1.07000,
            sl=1.06500,
            tp=1.08000,
        ),
    )
    request = session.sent[0]
    assert request["action"] == 7, "TRADE_ACTION_MODIFY"
    assert request["order"] == 88
    assert request["price"] == pytest.approx(1.07000)
    assert request["sl"] == pytest.approx(1.06500)
    assert request["tp"] == pytest.approx(1.08000)
    assert request["type_time"] == 0, "ORDER_TIME_GTC, matching how PLACE rests them"
    assert "volume" not in request, "MT5 cannot modify a pending order's volume"


def test_modifying_an_order_that_is_no_longer_resting_is_not_an_error() -> None:
    """It filled or it was removed; either way the order diff answers on its next tick."""
    session = FakeSession(resting_orders=set())
    result = run(session, cmd(op=ActionKind.MODIFY_ORDER, slave_ticket=88, entry=1.07))
    assert result.ok
    assert session.sent == []


def test_a_modify_whose_target_was_gone_is_flagged_so_belief_stays_put() -> None:
    """ok, because there was nothing left to get wrong -- but the levels landed on nothing,
    and the ledger must not record them as set."""
    gone_position = run(FakeSession(), modify())
    assert gone_position.ok and gone_position.target_gone

    gone_order = run(
        FakeSession(resting_orders=set()),
        cmd(op=ActionKind.MODIFY_ORDER, slave_ticket=88, entry=1.07),
    )
    assert gone_order.ok and gone_order.target_gone


def test_a_successful_modify_is_not_flagged_as_gone() -> None:
    assert not run(closing_session(), modify(sl=1.08250)).target_gone


def test_a_rejected_order_move_names_the_entry_as_well_as_the_levels() -> None:
    session = FakeSession(resting_orders={88})
    session.results = [FakeResult(retcode=int(Retcode.INVALID_PRICE))]
    result = run(session, cmd(op=ActionKind.MODIFY_ORDER, slave_ticket=88, entry=1.07, sl=1.065))
    assert not result.ok
    assert "1.07" in (result.error or "")
    assert "INVALID_PRICE" in (result.error or "")


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


def test_an_open_reports_its_opening_deals_commission_as_a_cost() -> None:
    session = FakeSession(commissions={777_001: -3.5})
    result = run(session)
    assert result.ok
    assert result.commission == pytest.approx(3.5)
    assert session.deals_read == [777_001]


@pytest.mark.parametrize(
    ("deal", "recorded"),
    [(777_001, None), (777_001, 0.0), (777_001, 1.2), (0, None), (-1, None)],
    ids=["unreadable", "free", "a rebate", "no deal", "the read raises"],
)
def test_an_open_whose_commission_shows_nothing_reports_none_and_still_succeeds(
    deal: int, recorded: float | None
) -> None:
    session = FakeSession(results=[FakeResult(deal=deal)])
    if recorded is not None:
        session.commissions[deal] = recorded
    result = run(session)
    assert result.ok
    assert result.commission is None


def test_a_resting_order_reads_no_commission_it_has_not_been_charged() -> None:
    session = FakeSession(commissions={777_001: -3.5})
    result = run(session, cmd(op=ActionKind.PLACE, order_type=OrderType.BUY_LIMIT, entry=1.07))
    assert result.ok
    assert result.commission is None
    assert session.deals_read == []


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


# --- symbol mapping ---------------------------------------------------------------------------


def test_an_explicit_mapping_places_the_order_on_the_mapped_symbol() -> None:
    session = FakeSession(symbols=("XAUUSDp", "XAUUSD.s"))
    result = run(
        session,
        cmd(master_symbol="XAUUSD", symbol_suffix="p", symbol_map=(("XAUUSD", "XAUUSD.s"),)),
    )
    assert result.ok
    assert result.slave_symbol == "XAUUSD.s"
    assert session.sent[0]["symbol"] == "XAUUSD.s"


def test_a_mapping_naming_a_symbol_the_broker_lacks_refuses_and_sends_nothing() -> None:
    """Falling through would place the trade on a name the user never wrote."""
    session = FakeSession(symbols=("EURGBPp",))
    result = run(session, cmd(master_symbol="EURGBP", symbol_map=(("EURGBP", "NOPE"),)))
    assert not result.ok
    assert session.sent == []


def test_the_configured_suffix_is_what_fixed_blackbull() -> None:
    session = FakeSession(symbols=("EURGBPp", "US30"))
    assert run(session, cmd(master_symbol="EURGBP", symbol_suffix="p")).slave_symbol == "EURGBPp"

    fresh = FakeSession(symbols=("EURGBPp", "US30"))
    assert run(fresh, cmd(master_symbol="US30", symbol_suffix="p")).slave_symbol == "US30", (
        "an undecorated index still resolves, so it needs no override"
    )


def test_a_new_suffix_is_honoured_without_restarting_the_worker() -> None:
    """One Executor, hence one symbol cache, per worker process -- and it used to answer from
    the key alone, so a corrected suffix was ignored until the process was restarted. That is
    the 2026-09-21 failure, and it is what makes an in-app editor worth having."""
    session = FakeSession(symbols=("EURGBPp",))
    executor = Executor(TID, session)

    refused = executor.execute(cmd(master_symbol="EURGBP"))
    assert not refused.ok, "no suffix configured yet"

    fixed = executor.execute(cmd(command_id=2, master_symbol="EURGBP", symbol_suffix="p"))
    assert fixed.ok
    assert fixed.slave_symbol == "EURGBPp"


def test_forgetting_the_cached_symbols_makes_the_next_command_re_read_them() -> None:
    """What a reconnect does, for the one input a cached answer cannot notice changing: the
    broker's own symbol list."""
    session = FakeSession(symbols=())
    executor = Executor(TID, session)
    assert not executor.execute(cmd()).ok

    session.symbols = ("EURUSD",)
    assert not executor.execute(cmd(command_id=2)).ok, "still the cached refusal"

    executor.forget_symbols()
    assert executor.execute(cmd(command_id=3)).ok


def test_a_filled_open_reports_the_symbols_point_for_slippage() -> None:
    result = run(FakeSession())
    assert result.ok
    assert result.point == 0.00001


def test_a_filled_open_reports_what_a_price_move_is_worth_per_lot() -> None:
    """Asked over a thousand points: $1,000 lost on one lot over 0.01 of EURUSD is $100,000
    per 1.0 of price. Breakeven turns the slave's commission into a distance with it."""
    result = run(FakeSession(loss=1_000.0))
    assert result.price_value == pytest.approx(100_000.0)


def test_an_unpriceable_move_does_not_fail_a_fill() -> None:
    session = FakeSession()
    executor = Executor(TID, session)
    session.loss = None  # sizing has already been done by now; only the pricing fails
    result = executor._price_value(PositionType.BUY, "EURUSD", 1.085, 0.00001)
    assert result is None


def test_a_refused_open_carries_no_point() -> None:
    result = run(FakeSession(results=[FakeResult(retcode=int(Retcode.INVALID_STOPS))]))
    assert not result.ok
    assert result.point is None


# --- booking profit at breakeven -------------------------------------------------------------


def test_a_booking_closes_its_fraction_of_the_live_volume() -> None:
    session = closing_session(volume=1.0)
    result = run(session, cmd(op=ActionKind.BOOK, slave_ticket=77, close_fraction=0.5))
    assert result.ok
    assert session.sent[0]["volume"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("volume", "fraction", "why"),
    [
        (0.01, 0.5, "the slice is under the minimum lot"),
        (0.03, 0.2, "a fifth of 0.03 is under the minimum lot too"),
    ],
)
def test_a_booking_that_cannot_be_cut_closes_nothing_and_says_so(
    volume: float, fraction: float, why: str
) -> None:
    session = closing_session(volume=volume)
    session.facts = SymbolFacts(
        name="EURUSD",
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        point=0.00001,
        filling_mask=int(SymbolFilling.FOK | SymbolFilling.IOC),
    )
    result = run(session, cmd(op=ActionKind.BOOK, slave_ticket=77, close_fraction=fraction))
    assert not result.ok and result.unsplittable, why
    assert session.sent == []


def test_a_booking_that_would_leave_less_than_the_minimum_closes_nothing() -> None:
    session = closing_session(volume=0.15)
    session.facts = SymbolFacts(
        name="EURUSD",
        volume_min=0.10,
        volume_max=100.0,
        volume_step=0.01,
        point=0.00001,
        filling_mask=int(SymbolFilling.FOK | SymbolFilling.IOC),
    )
    result = run(session, cmd(op=ActionKind.BOOK, slave_ticket=77, close_fraction=0.7))
    assert not result.ok and result.unsplittable
    assert "would leave 0.05" in (result.error or "")
    assert session.sent == []
