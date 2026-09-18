"""Diff engine tests.

The point of Stage 2a is that this layer is provable without a terminal, a broker or a network,
so every test here is pure data in and data out. If the copier's decisions are wrong, they are
wrong here, and they cost nothing to get wrong here.

The engine-level tests at the bottom instantiate an :class:`Engine` but never start it, so no
worker is spawned; they exercise only the guards that decide whether reconciliation may run at
all.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mt5copier.config import AppConfig, NoStopPolicy, Role, TerminalConfig
from mt5copier.engine import Engine
from mt5copier.protocol import (
    OrderResult,
    OrderType,
    PendingOrder,
    Position,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.reconcile import (
    Action,
    ActionKind,
    CopiedOrder,
    CopiedPosition,
    CopyLedger,
    SizingBasis,
    SkipReason,
    SlaveLink,
    invert_order_type,
    map_symbol,
    reconcile_slave,
    reconcile_slave_orders,
)
from mt5copier.store import MappingStore

TID_MASTER = "A" * 32
TID_SLAVE = "B" * 32
TID_SLAVE2 = "C" * 32


def pos(
    ticket: int = 1,
    symbol: str = "EURUSD",
    ptype: PositionType = PositionType.BUY,
    volume: float = 1.0,
    price_open: float = 1.08500,
    sl: float = 1.08000,
    tp: float = 0.0,
) -> Position:
    return Position(
        ticket=ticket,
        symbol=symbol,
        type=ptype,
        volume=volume,
        price_open=price_open,
        sl=sl,
        tp=tp,
        time=1_700_000_000,
        magic=0,
        comment="",
    )


def slave(**kw: object) -> TerminalConfig:
    base: dict[str, object] = {
        "terminal_id": TID_SLAVE,
        "label": "Slave",
        "role": Role.SLAVE,
    }
    base.update(kw)
    return TerminalConfig(**base)  # type: ignore[arg-type]


def copied(
    ticket: int = 1,
    symbol: str = "EURUSD",
    ptype: PositionType = PositionType.BUY,
    master_volume: float = 1.0,
    sl: float = 1.08000,
    tp: float = 0.0,
    terminal_id: str = TID_SLAVE,
) -> CopiedPosition:
    return CopiedPosition(
        terminal_id=terminal_id,
        master_ticket=ticket,
        slave_symbol=symbol,
        type=ptype,
        master_volume=master_volume,
        synced_sl=sl,
        synced_tp=tp,
    )


# --- opens ----------------------------------------------------------------------------------


def test_new_master_position_is_an_open() -> None:
    actions = reconcile_slave([pos(ticket=42)], [], slave())
    assert len(actions) == 1
    a = actions[0]
    assert a.kind is ActionKind.OPEN
    assert a.master_ticket == 42
    assert a.terminal_id == TID_SLAVE
    assert a.executable
    assert a.volume is None, "sizing belongs to stage 2b, not here"
    assert a.master_volume == 1.0


def test_open_without_stop_loss_is_skipped_by_default() -> None:
    actions = reconcile_slave([pos(sl=0.0)], [], slave())
    assert actions[0].kind is ActionKind.OPEN
    assert not actions[0].executable
    assert actions[0].skip_reason is SkipReason.NO_STOP_LOSS


def test_a_stopped_trade_is_sized_from_the_slaves_own_risk() -> None:
    action = reconcile_slave([pos(sl=1.08)], [], slave())[0]
    assert action.sizing_basis is SizingBasis.RISK_FROM_STOP
    assert action.lot_multiple is None
    assert not action.unprotected


def test_lot_multiple_policy_copies_an_unstopped_trade_off_the_masters_size() -> None:
    cfg = slave(no_stop_policy=NoStopPolicy.LOT_MULTIPLE, no_stop_lot_multiple=0.5)
    action = reconcile_slave([pos(volume=2.0, sl=0.0)], [], cfg)[0]
    assert action.executable
    assert action.sizing_basis is SizingBasis.MASTER_LOT_MULTIPLE
    assert action.lot_multiple == 0.5
    assert action.master_volume == 2.0
    assert action.unprotected, "the copy still opens with no stop of its own"


def test_the_lot_multiple_target_is_spelled_out_in_the_log() -> None:
    cfg = slave(no_stop_policy=NoStopPolicy.LOT_MULTIPLE, no_stop_lot_multiple=0.5)
    line = reconcile_slave([pos(volume=2.0, sl=0.0)], [], cfg)[0].describe()
    assert "0.5 x master 2 = 1 lots" in line
    assert "UNPROTECTED" in line


def test_a_filter_still_beats_the_lot_multiple_policy() -> None:
    cfg = slave(no_stop_policy=NoStopPolicy.LOT_MULTIPLE, symbol_blacklist=["EURUSD"])
    action = reconcile_slave([pos(sl=0.0)], [], cfg)[0]
    assert action.skip_reason is SkipReason.SYMBOL_BLACKLISTED
    assert not action.unprotected, "nothing opens, so nothing is unprotected"


def test_a_stop_of_zero_is_what_mt5_means_by_no_stop() -> None:
    assert not pos(sl=0.0).has_stop
    assert pos(sl=1.08).has_stop


def test_blacklisted_symbol_is_skipped() -> None:
    actions = reconcile_slave([pos(symbol="XAUUSD")], [], slave(symbol_blacklist=["xauusd"]))
    assert actions[0].skip_reason is SkipReason.SYMBOL_BLACKLISTED


def test_whitelist_excludes_everything_else() -> None:
    cfg = slave(symbol_whitelist=["EURUSD"])
    assert reconcile_slave([pos(symbol="EURUSD")], [], cfg)[0].executable
    assert (
        reconcile_slave([pos(symbol="GBPUSD")], [], cfg)[0].skip_reason
        is SkipReason.SYMBOL_NOT_WHITELISTED
    )


def test_an_empty_whitelist_allows_everything() -> None:
    assert reconcile_slave([pos(symbol="GBPUSD")], [], slave())[0].executable


def test_filters_are_checked_before_the_stop_loss() -> None:
    """A blacklisted symbol with no stop should report the filter, not the missing stop --
    the filter is the user's own decision and is the more useful thing to show."""
    actions = reconcile_slave(
        [pos(symbol="XAUUSD", sl=0.0)], [], slave(symbol_blacklist=["XAUUSD"])
    )
    assert actions[0].skip_reason is SkipReason.SYMBOL_BLACKLISTED


def test_inversion_flips_the_direction() -> None:
    actions = reconcile_slave([pos(ptype=PositionType.BUY)], [], slave(invert=True))
    assert actions[0].type is PositionType.SELL


def test_inversion_swaps_stop_and_target() -> None:
    """The master's stop is where its trade fails, which is where the reverse succeeds. Passing
    the levels through unswapped would put the short's stop below its entry."""
    master = pos(ptype=PositionType.BUY, price_open=1.0850, sl=1.0800, tp=1.0950)
    action = reconcile_slave([master], [], slave(invert=True))[0]
    assert action.type is PositionType.SELL
    assert action.sl == pytest.approx(1.0950), "stop goes above entry for a short"
    assert action.tp == pytest.approx(1.0800)


def test_a_normal_copy_does_not_swap() -> None:
    action = reconcile_slave([pos(sl=1.08, tp=1.095)], [], slave())[0]
    assert (action.sl, action.tp) == (pytest.approx(1.08), pytest.approx(1.095))


def test_an_inverted_copy_is_sized_to_the_masters_target() -> None:
    """Its risk runs to where the master takes profit, not to the master's stop."""
    master = pos(ptype=PositionType.BUY, sl=1.0800, tp=1.0950)
    action = reconcile_slave([master], [], slave(invert=True))[0]
    assert action.sizing_basis is SizingBasis.RISK_FROM_STOP
    assert action.sl == pytest.approx(1.0950)


def test_inverting_a_trade_with_no_target_leaves_the_copy_unstopped() -> None:
    """A stop but no TP inverts into no stop at all, so the no-stop policy decides -- which by
    default means a slave set to invert copies nothing from a master that sets no targets."""
    master = pos(sl=1.0800, tp=0.0)
    assert reconcile_slave([master], [], slave(invert=True))[0].skip_reason is (
        SkipReason.NO_STOP_LOSS
    )

    permissive = slave(invert=True, no_stop_policy=NoStopPolicy.LOT_MULTIPLE)
    action = reconcile_slave([master], [], permissive)[0]
    assert action.executable
    assert action.unprotected


def test_an_inverted_sltp_change_is_detected_in_slave_terms() -> None:
    cfg = slave(invert=True)
    copies = [copied(ptype=PositionType.SELL, sl=1.0950, tp=1.0800)]
    assert reconcile_slave([pos(sl=1.0800, tp=1.0950)], copies, cfg) == []

    moved = reconcile_slave([pos(sl=1.0800, tp=1.0960)], copies, cfg)
    assert [a.kind for a in moved] == [ActionKind.MODIFY_SLTP]
    assert moved[0].sl == pytest.approx(1.0960), "the master's new target is the copy's stop"


def test_prefix_and_suffix_are_applied_to_the_symbol() -> None:
    cfg = slave(symbol_prefix="m", symbol_suffix=".r")
    assert map_symbol("EURUSD", cfg) == "mEURUSD.r"
    a = reconcile_slave([pos(symbol="EURUSD")], [], cfg)[0]
    assert a.master_symbol == "EURUSD"
    assert a.slave_symbol == "mEURUSD.r"


# --- closes and changes ---------------------------------------------------------------------


def test_a_copy_whose_master_position_vanished_is_a_close() -> None:
    actions = reconcile_slave([], [copied(ticket=7)], slave())
    assert len(actions) == 1
    assert actions[0].kind is ActionKind.CLOSE
    assert actions[0].master_ticket == 7
    assert actions[0].executable, "a full close is never conditional"


def test_closes_are_emitted_before_opens() -> None:
    actions = reconcile_slave([pos(ticket=2)], [copied(ticket=1)], slave())
    assert [a.kind for a in actions] == [ActionKind.CLOSE, ActionKind.OPEN]


def test_partial_close_gives_the_fraction_to_mirror() -> None:
    actions = reconcile_slave([pos(volume=0.60)], [copied(master_volume=1.0)], slave())
    assert actions[0].kind is ActionKind.REDUCE
    assert actions[0].close_fraction == pytest.approx(0.40)
    assert actions[0].executable


def test_partial_close_fraction_is_measured_against_the_sized_volume() -> None:
    """Not against the previous tick. A missed tick must still land on the right ratio."""
    actions = reconcile_slave([pos(volume=0.25)], [copied(master_volume=1.0)], slave())
    assert actions[0].close_fraction == pytest.approx(0.75)


def test_partials_can_be_turned_off_per_slave() -> None:
    actions = reconcile_slave(
        [pos(volume=0.5)], [copied(master_volume=1.0)], slave(copy_partials=False)
    )
    assert actions[0].kind is ActionKind.REDUCE
    assert actions[0].skip_reason is SkipReason.PARTIALS_DISABLED


def test_a_growing_volume_is_never_mirrored() -> None:
    """Unreachable on a hedging account -- a further entry is a new ticket, not a bigger one.
    Nothing is mirrored for it, and the copy stays anchored to what it was sized against."""
    assert reconcile_slave([pos(volume=2.0)], [copied(master_volume=1.0)], slave()) == []


def test_a_growing_volume_is_warned_about_once(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5, volume=1.0)], [link()], now=clock.advance(1.0))

    with caplog.at_level("WARNING"):
        assert ledger.step([pos(ticket=5, volume=2.0)], [link()], now=clock.advance(1.0)) == []
        assert ledger.step([pos(ticket=5, volume=2.0)], [link()], now=clock.advance(1.0)) == []
    warnings = [r for r in caplog.records if "should be impossible" in r.message]
    assert len(warnings) == 1, "warn once, not on every tick"
    assert ledger.believed()[0].master_volume == pytest.approx(1.0)


def test_a_partial_after_a_growth_still_uses_the_sized_volume() -> None:
    """The ratio must stay anchored to what the slave was actually sized against, or an
    impossible growth would silently corrupt every later partial."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5, volume=1.0)], [link()], now=clock.advance(1.0))
    ledger.step([pos(ticket=5, volume=2.0)], [link()], now=clock.advance(1.0))

    reduced = ledger.step([pos(ticket=5, volume=0.5)], [link()], now=clock.advance(1.0))
    assert reduced[0].close_fraction == pytest.approx(0.5)


def test_stop_loss_change_is_a_modify() -> None:
    actions = reconcile_slave([pos(sl=1.08300)], [copied(sl=1.08000)], slave())
    assert actions[0].kind is ActionKind.MODIFY_SLTP
    assert actions[0].sl == pytest.approx(1.08300)


def test_take_profit_change_is_a_modify() -> None:
    actions = reconcile_slave([pos(tp=1.09500)], [copied(tp=0.0)], slave())
    assert actions[0].kind is ActionKind.MODIFY_SLTP
    assert actions[0].tp == pytest.approx(1.09500)


def test_sltp_sync_can_be_turned_off_per_slave() -> None:
    actions = reconcile_slave([pos(sl=1.09)], [copied(sl=1.08)], slave(copy_sltp=False))
    assert actions[0].skip_reason is SkipReason.SLTP_SYNC_DISABLED


def test_an_unchanged_position_produces_nothing() -> None:
    assert reconcile_slave([pos()], [copied()], slave()) == []


def test_float_noise_is_not_a_change() -> None:
    assert reconcile_slave([pos(volume=1.0 + 1e-12, sl=1.08 + 1e-12)], [copied()], slave()) == []


def test_a_volume_and_an_sltp_change_in_one_tick_give_two_actions() -> None:
    actions = reconcile_slave([pos(volume=0.5, sl=1.09)], [copied(master_volume=1.0)], slave())
    assert [a.kind for a in actions] == [ActionKind.REDUCE, ActionKind.MODIFY_SLTP]


def test_output_order_is_deterministic_regardless_of_input_order() -> None:
    positions = [pos(ticket=3), pos(ticket=1), pos(ticket=2)]
    first = reconcile_slave(positions, [], slave())
    second = reconcile_slave(list(reversed(positions)), [], slave())
    assert [a.master_ticket for a in first] == [1, 2, 3]
    assert first == second


def test_describe_mentions_the_skip_reason() -> None:
    line = reconcile_slave([pos(sl=0.0)], [], slave())[0].describe()
    assert "SKIPPED" in line
    assert "no stop loss" in line


# --- exclusion of pre-existing positions ----------------------------------------------------


def test_a_position_already_open_is_never_opened() -> None:
    actions = reconcile_slave([pos(ticket=5)], [], slave(), excluded={5})
    assert actions == []


def test_exclusion_does_not_block_closing_something_we_copied() -> None:
    """Exclusion only gates opens. Anything already believed stays fully mirrored."""
    actions = reconcile_slave([], [copied(ticket=5)], slave(), excluded={5})
    assert [a.kind for a in actions] == [ActionKind.CLOSE]


def test_exclusion_is_per_ticket_not_per_symbol() -> None:
    master = [pos(ticket=5, symbol="EURUSD"), pos(ticket=6, symbol="EURUSD")]
    actions = reconcile_slave(master, [], slave(), excluded={5})
    assert [a.master_ticket for a in actions] == [6]


# --- pending orders -------------------------------------------------------------------------


def order(
    ticket: int = 50,
    symbol: str = "EURUSD",
    otype: OrderType = OrderType.BUY_LIMIT,
    volume: float = 1.0,
    price_open: float = 1.08000,
    sl: float = 1.07500,
    tp: float = 0.0,
) -> PendingOrder:
    return PendingOrder(
        ticket=ticket,
        symbol=symbol,
        type=otype,
        volume=volume,
        price_open=price_open,
        sl=sl,
        tp=tp,
        time_setup=1_700_000_000,
    )


def copied_order(
    ticket: int = 50,
    symbol: str = "EURUSD",
    otype: OrderType = OrderType.BUY_LIMIT,
    entry: float = 1.08000,
    sl: float = 1.07500,
    tp: float = 0.0,
) -> CopiedOrder:
    return CopiedOrder(
        terminal_id=TID_SLAVE,
        master_ticket=ticket,
        slave_symbol=symbol,
        order_type=otype,
        master_volume=1.0,
        entry=entry,
        synced_sl=sl,
        synced_tp=tp,
    )


def test_a_resting_order_is_placed_when_pending_copying_is_on() -> None:
    action = reconcile_slave_orders([order()], [], [], slave(copy_pending=True))[0]
    assert action.kind is ActionKind.PLACE
    assert action.order_type is OrderType.BUY_LIMIT
    assert action.type is PositionType.BUY
    assert action.executable


def test_pending_copying_is_off_by_default() -> None:
    action = reconcile_slave_orders([order()], [], [], slave())[0]
    assert action.kind is ActionKind.PLACE
    assert action.skip_reason is SkipReason.PENDING_DISABLED


def test_market_orders_in_flight_are_not_treated_as_resting() -> None:
    in_flight = order(otype=OrderType.BUY)
    assert reconcile_slave_orders([in_flight], [], [], slave(copy_pending=True)) == []


def test_stop_limit_orders_are_not_mirrored() -> None:
    """Two prices to honour, so a wrong mirror is worse than none."""
    weird = order(otype=OrderType.BUY_STOP_LIMIT)
    action = reconcile_slave_orders([weird], [], [], slave(copy_pending=True))[0]
    assert action.skip_reason is SkipReason.STOP_LIMIT_UNSUPPORTED


def test_moving_a_resting_orders_entry_is_a_modify() -> None:
    """The change a position can never have."""
    actions = reconcile_slave_orders(
        [order(price_open=1.07900)], [], [copied_order(entry=1.08000)], slave(copy_pending=True)
    )
    assert [a.kind for a in actions] == [ActionKind.MODIFY_ORDER]
    assert actions[0].entry == pytest.approx(1.07900)


def test_an_unchanged_resting_order_produces_nothing() -> None:
    assert reconcile_slave_orders([order()], [], [copied_order()], slave(copy_pending=True)) == []


def test_an_orders_volume_change_is_not_a_modify() -> None:
    """MT5 cannot modify an order's volume, so a change means a partial fill or a replace."""
    actions = reconcile_slave_orders(
        [order(volume=0.3)], [], [copied_order()], slave(copy_pending=True)
    )
    assert actions == []


def test_a_vanished_order_with_no_matching_position_is_a_cancel() -> None:
    actions = reconcile_slave_orders([], [], [copied_order(ticket=50)], slave(copy_pending=True))
    assert [a.kind for a in actions] == [ActionKind.CANCEL]
    assert actions[0].executable


def test_a_vanished_order_whose_ticket_is_now_a_position_was_filled() -> None:
    """MT5 gives the position the filling order's own ticket. Mistaking this for a cancel
    would delete a slave order that was about to fill -- silently dropping the trade."""
    actions = reconcile_slave_orders([], [50], [copied_order(ticket=50)], slave(copy_pending=True))
    assert [a.kind for a in actions] == [ActionKind.ORDER_FILLED]
    assert not actions[0].executable, "the slave's own order fills; nothing is sent"
    assert actions[0].skip_reason is SkipReason.NOTHING_TO_SEND


def test_inverting_a_buy_limit_gives_a_sell_stop() -> None:
    """Both the direction and the limit/stop kind flip, so the price level is preserved. A
    SELL_LIMIT at a level below the market would be rejected outright."""
    assert invert_order_type(OrderType.BUY_LIMIT) is OrderType.SELL_STOP
    assert invert_order_type(OrderType.SELL_LIMIT) is OrderType.BUY_STOP
    assert invert_order_type(OrderType.BUY_STOP) is OrderType.SELL_LIMIT
    assert invert_order_type(OrderType.SELL_STOP) is OrderType.BUY_LIMIT
    assert invert_order_type(OrderType.BUY_STOP_LIMIT) is None


def test_an_inverted_order_keeps_its_price_and_flips_its_kind() -> None:
    cfg = slave(copy_pending=True, invert=True)
    action = reconcile_slave_orders(
        [order(otype=OrderType.BUY_LIMIT, price_open=1.08, sl=1.075, tp=1.09)], [], [], cfg
    )[0]
    assert action.order_type is OrderType.SELL_STOP
    assert action.type is PositionType.SELL
    assert action.entry == pytest.approx(1.08), "the level is the whole point"
    assert action.sl == pytest.approx(1.09), "stop and target swap, as for a position"
    assert action.tp == pytest.approx(1.075)


def test_an_excluded_order_is_silent() -> None:
    assert reconcile_slave_orders([order(ticket=50)], [], [], slave(copy_pending=True), {50}) == []


# --- the ledger -----------------------------------------------------------------------------


def link(connected: bool = True, **kw: object) -> SlaveLink:
    return SlaveLink(cfg=slave(**kw), connected=connected)


class Clock:
    """Explicit monotonic time, so continuity and lapses are exact rather than timing-dependent."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def watching(ledger: CopyLedger, clock: Clock, links: list[SlaveLink] | None = None) -> None:
    """Establish continuous observation against a flat master.

    Every ledger test needs this first: without a prior tick, the next one re-baselines, which
    is the whole point of the rule being tested.
    """
    ledger.step([], links or [link()], now=clock.advance(1.0))


def test_a_position_open_before_we_were_watching_is_reported_and_never_copied() -> None:
    clock = Clock()
    ledger = CopyLedger()
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))

    assert len(actions) == 1
    assert actions[0].skip_reason is SkipReason.PREEXISTING
    assert not actions[0].executable
    assert ledger.believed() == []

    # and it is never mentioned again, nor copied, however long it stays open
    assert ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0)) == []
    assert ledger.believed() == []


def test_a_preexisting_position_is_not_copied_when_the_master_moves_its_stop() -> None:
    """The re-report-on-change mechanism must not become a back door into adoption."""
    clock = Clock()
    ledger = CopyLedger()
    ledger.step([pos(ticket=5, sl=1.08)], [link()], now=clock.advance(1.0))
    assert ledger.step([pos(ticket=5, sl=1.09)], [link()], now=clock.advance(1.0)) == []
    assert ledger.believed() == []


def test_a_position_opened_while_we_watch_is_copied() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)

    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))
    assert [a.kind for a in actions] == [ActionKind.OPEN]
    assert actions[0].executable
    assert [c.master_ticket for c in ledger.believed()] == [5]


def test_an_open_is_proposed_once_not_on_every_tick() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    master = [pos(ticket=5)]
    assert len(ledger.step(master, [link()], now=clock.advance(1.0))) == 1
    assert ledger.step(master, [link()], now=clock.advance(1.0)) == []
    assert ledger.step(master, [link()], now=clock.advance(1.0)) == []


def test_a_skipped_open_is_not_believed() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(sl=0.0)], [link()], now=clock.advance(1.0))
    assert ledger.believed() == []


def test_a_decline_is_final_even_when_a_stop_arrives_later() -> None:
    """Attaching a stop five minutes on does not make the trade copyable -- it makes it a late
    join, and the slave would fill far from the master's entry with the risk sizing a fiction."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)

    first = ledger.step([pos(ticket=9, sl=0.0)], [link()], now=clock.advance(1.0))
    assert [a.skip_reason for a in first] == [SkipReason.NO_STOP_LOSS]
    assert ledger.step([pos(ticket=9, sl=0.0)], [link()], now=clock.advance(1.0)) == []

    later = ledger.step([pos(ticket=9, sl=1.079)], [link()], now=clock.advance(1.0))
    assert later == []
    assert ledger.believed() == []


def test_a_decline_is_final_for_a_filtered_symbol_too() -> None:
    """Same rule, any reason. Widening a whitelist does not retroactively adopt open trades."""
    clock = Clock()
    ledger = CopyLedger()
    blocked = [link(symbol_whitelist=["GBPUSD"])]
    watching(ledger, clock, blocked)

    first = ledger.step([pos(ticket=9, symbol="EURUSD")], blocked, now=clock.advance(1.0))
    assert [a.skip_reason for a in first] == [SkipReason.SYMBOL_NOT_WHITELISTED]

    allowed = [link(symbol_whitelist=["GBPUSD", "EURUSD"])]
    assert ledger.step([pos(ticket=9, symbol="EURUSD")], allowed, now=clock.advance(1.0)) == []
    assert ledger.believed() == []


def test_a_declined_ticket_is_forgotten_once_the_master_closes_it() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=9, sl=0.0)], [link()], now=clock.advance(1.0))
    assert ledger._excluded[TID_SLAVE] == {9}

    ledger.step([], [link()], now=clock.advance(1.0))
    assert ledger._excluded[TID_SLAVE] == set()


def test_close_is_mirrored_once_then_forgotten() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))

    closed = ledger.step([], [link()], now=clock.advance(1.0))
    assert [a.kind for a in closed] == [ActionKind.CLOSE]
    assert ledger.believed() == []
    assert ledger.step([], [link()], now=clock.advance(1.0)) == []


def test_partial_is_mirrored_then_settles() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5, volume=1.0)], [link()], now=clock.advance(1.0))

    reduced = ledger.step([pos(ticket=5, volume=0.4)], [link()], now=clock.advance(1.0))
    assert reduced[0].close_fraction == pytest.approx(0.6)
    assert ledger.step([pos(ticket=5, volume=0.4)], [link()], now=clock.advance(1.0)) == []
    assert ledger.believed()[0].master_volume == pytest.approx(0.4)


def test_successive_sltp_changes_are_each_mirrored() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5, sl=1.08)], [link()], now=clock.advance(1.0))

    for target in (1.081, 1.082):
        actions = ledger.step([pos(ticket=5, sl=target)], [link()], now=clock.advance(1.0))
        assert [a.kind for a in actions] == [ActionKind.MODIFY_SLTP]
        assert actions[0].sl == pytest.approx(target)
        assert ledger.step([pos(ticket=5, sl=target)], [link()], now=clock.advance(1.0)) == []


def test_slaves_are_independent() -> None:
    clock = Clock()
    ledger = CopyLedger()
    links = [link(), link(terminal_id=TID_SLAVE2, label="Slave2", symbol_blacklist=["EURUSD"])]
    watching(ledger, clock, links)

    actions = ledger.step([pos(ticket=5)], links, now=clock.advance(1.0))
    by_tid = {a.terminal_id: a for a in actions}
    assert by_tid[TID_SLAVE].executable
    assert by_tid[TID_SLAVE2].skip_reason is SkipReason.SYMBOL_BLACKLISTED
    assert [c.terminal_id for c in ledger.believed()] == [TID_SLAVE]


def test_a_slave_that_lost_its_role_gets_no_actions_but_keeps_its_records() -> None:
    """Deleting the records would abandon real open positions -- untracked, unclosed, and
    indistinguishable from a stranger's trades next startup. Unassigning means "stop acting",
    nothing more."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))

    assert ledger.step([pos(ticket=5)], [], now=clock.advance(1.0)) == []
    assert len(ledger.believed()) == 1


# --- the order lifecycle, through the ledger ------------------------------------------------


def pending_link(**kw: object) -> SlaveLink:
    return link(copy_pending=True, **kw)


def test_a_resting_order_placed_while_we_watch_is_mirrored_once() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock, [pending_link()])

    placed = ledger.step(
        [], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0)
    )
    assert [a.kind for a in placed] == [ActionKind.PLACE]
    assert [c.master_ticket for c in ledger.believed_orders()] == [50]
    assert (
        ledger.step([], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0))
        == []
    )


def test_an_order_resting_before_we_watched_is_never_placed() -> None:
    clock = Clock()
    ledger = CopyLedger()
    actions = ledger.step(
        [], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0)
    )
    assert [a.skip_reason for a in actions] == [SkipReason.PREEXISTING]
    assert ledger.believed_orders() == []


def test_cancelling_on_the_master_cancels_the_copy() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock, [pending_link()])
    ledger.step([], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0))

    cancelled = ledger.step([], [pending_link()], master_orders=[], now=clock.advance(1.0))
    assert [a.kind for a in cancelled] == [ActionKind.CANCEL]
    assert ledger.believed_orders() == []


def test_a_fill_hands_tracking_to_the_position_without_copying_it_twice() -> None:
    """The slave's own order fills at the same price, so no OPEN may be sent -- but the
    position that results is ours and its close must still be mirrored."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock, [pending_link()])
    ledger.step([], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0))

    filled = ledger.step(
        [pos(ticket=50)], [pending_link()], master_orders=[], now=clock.advance(1.0)
    )
    assert [a.kind for a in filled] == [ActionKind.ORDER_FILLED]
    assert ledger.believed_orders() == []
    assert [c.master_ticket for c in ledger.believed()] == [50], "now tracked as a position"

    assert ledger.step([pos(ticket=50)], [pending_link()], now=clock.advance(1.0)) == []
    closed = ledger.step([], [pending_link()], now=clock.advance(1.0))
    assert [a.kind for a in closed] == [ActionKind.CLOSE]


def test_with_pending_copying_off_the_fill_is_still_copied_as_a_position() -> None:
    """Declining to mirror the resting order says nothing about the position it becomes --
    that is a fresh entry at a current price, judged on its own."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([], [link()], master_orders=[order(ticket=50)], now=clock.advance(1.0))
    assert ledger.believed_orders() == []

    filled = ledger.step([pos(ticket=50)], [link()], master_orders=[], now=clock.advance(1.0))
    assert [a.kind for a in filled] == [ActionKind.OPEN]
    assert filled[0].executable


def test_moving_an_order_then_leaving_it_alone_settles() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock, [pending_link()])
    ledger.step(
        [],
        [pending_link()],
        master_orders=[order(ticket=50, price_open=1.08)],
        now=clock.advance(1.0),
    )

    moved = ledger.step(
        [],
        [pending_link()],
        master_orders=[order(ticket=50, price_open=1.079)],
        now=clock.advance(1.0),
    )
    assert [a.kind for a in moved] == [ActionKind.MODIFY_ORDER]
    assert (
        ledger.step(
            [],
            [pending_link()],
            master_orders=[order(ticket=50, price_open=1.079)],
            now=clock.advance(1.0),
        )
        == []
    )


def test_order_state_survives_a_restart(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "beliefs.db"
    with MappingStore(path) as store:
        ledger = CopyLedger(store)
        watching(ledger, clock, [pending_link()])
        ledger.step([], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0))

    with MappingStore(path) as store:
        revived = CopyLedger(store)
        assert len(revived.believed_orders()) == 1
        assert (
            revived.step(
                [], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0)
            )
            == []
        )


# --- live mode: in-flight commands and outcome-driven belief ---------------------------------


def result(
    action: Action,
    ok: bool = True,
    slave_ticket: int | None = 900,
    filled: float | None = 1.0,
) -> OrderResult:
    return OrderResult(
        command_id=1,
        terminal_id=action.terminal_id,
        op=action.kind,
        master_ticket=action.master_ticket,
        ok=ok,
        slave_ticket=slave_ticket,
        slave_symbol=action.slave_symbol,
        filled_volume=filled,
        error=None if ok else "no money",
    )


def test_live_mode_does_not_believe_an_open_until_it_fills() -> None:
    """Belief follows the broker, not the intention. An OPEN we sent and an OPEN that filled are
    different facts."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)

    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0), simulate=False)
    assert [a.kind for a in actions] == [ActionKind.OPEN]
    assert ledger.believed() == [], "not yet -- nothing has filled"
    assert ledger.inflight_count == 1

    ledger.apply_result(result(actions[0]))
    assert [c.master_ticket for c in ledger.believed()] == [5]
    assert ledger.inflight_count == 0


def test_an_open_in_flight_is_never_sent_twice() -> None:
    """The hazard that only exists live: the master's state has not changed, so the diff derives
    the same OPEN again -- and acting on it would place a second order against one position."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    master = [pos(ticket=5)]

    first = ledger.step(master, [link()], now=clock.advance(1.0), simulate=False)
    assert len(first) == 1

    # Several ticks pass with no answer from the broker.
    for _ in range(3):
        assert ledger.step(master, [link()], now=clock.advance(1.0), simulate=False) == []
    assert ledger.inflight_count == 1


def test_a_changed_decision_is_still_withheld_while_in_flight() -> None:
    """Even a genuinely new decision must wait: the outstanding order may already have filled."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5, sl=1.08)], [link()], now=clock.advance(1.0), simulate=False)

    moved = ledger.step([pos(ticket=5, sl=1.081)], [link()], now=clock.advance(1.0), simulate=False)
    assert moved == []


def test_the_filled_volume_is_believed_not_the_requested_one() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0), simulate=False)
    ledger.apply_result(result(actions[0], filled=0.37, slave_ticket=901))

    believed = ledger.believed()[0]
    assert believed.volume == pytest.approx(0.37)
    assert believed.slave_ticket == 901


def test_a_failed_open_is_final_and_not_retried() -> None:
    """A permanent failure re-attempted on every stop tweak, or a transient one re-attempted
    long after the price moved, both place a trade that is no longer the master's."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0), simulate=False)
    ledger.apply_result(result(actions[0], ok=False))

    assert ledger.believed() == []
    assert ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0), simulate=False) == []
    later = ledger.step([pos(ticket=5, sl=1.09)], [link()], now=clock.advance(1.0), simulate=False)
    assert later == [], "not even when the master moves its stop"


def test_a_close_is_believed_gone_only_once_confirmed() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    opened = ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0), simulate=False)
    ledger.apply_result(result(opened[0]))

    closing = ledger.step([], [link()], now=clock.advance(1.0), simulate=False)
    assert [a.kind for a in closing] == [ActionKind.CLOSE]
    assert len(ledger.believed()) == 1, "still ours until the close is confirmed"

    ledger.apply_result(result(closing[0]))
    assert ledger.believed() == []


def test_an_unanswered_command_is_given_up_on_and_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worker can die between taking a command and reporting. Leaving it in flight freezes
    the ticket forever; clearing it silently could double-send."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0), simulate=False)
    assert ledger.inflight_count == 1

    with caplog.at_level("ERROR"):
        lost = ledger.expire_inflight(timeout_s=-1.0)
    assert lost == [(TID_SLAVE, 5)]
    assert ledger.inflight_count == 0
    assert any("orphan" in r.getMessage() for r in caplog.records)

    assert ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0), simulate=False) == []


def test_a_result_with_no_pending_action_is_left_to_reconciliation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restart between sending and the answer. Belief cannot be reconstructed from the result
    alone, and inventing it would be worse than letting it surface as an orphan."""
    ledger = CopyLedger()
    stray = OrderResult(
        command_id=9,
        terminal_id=TID_SLAVE,
        op=ActionKind.OPEN,
        master_ticket=5,
        ok=True,
        slave_ticket=900,
    )
    with caplog.at_level("WARNING"):
        ledger.apply_result(stray)
    assert ledger.believed() == []
    assert any("startup reconciliation" in r.getMessage() for r in caplog.records)


def test_resetting_session_state_keeps_belief() -> None:
    """Belief lives in the store and is swapped with it; exclusions and continuity do not."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))
    assert len(ledger.believed()) == 1

    ledger.reset_session_state()
    assert len(ledger.believed()) == 1
    assert ledger.inflight_count == 0


# --- connection gating and the grace window -------------------------------------------------


def test_a_disconnected_slave_gets_no_actions_at_all() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    assert ledger.step([pos(ticket=5)], [link(connected=False)], now=clock.advance(1.0)) == []
    assert ledger.believed() == []


def test_a_brief_blip_does_not_cost_a_trade() -> None:
    """Three seconds down, master opens in the gap: still copied when the link returns."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)

    ledger.step([], [link(connected=False)], now=clock.advance(3.0))
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(0.5), grace_seconds=5.0)
    assert [a.kind for a in actions] == [ActionKind.OPEN]
    assert actions[0].executable


def test_a_long_outage_excludes_what_opened_during_it() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)

    ledger.step([], [link(connected=False)], now=clock.advance(60.0))
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0), grace_seconds=5.0)
    assert [a.skip_reason for a in actions] == [SkipReason.PREEXISTING]
    assert ledger.believed() == []


def test_the_grace_boundary_is_inclusive() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(5.0), grace_seconds=5.0)
    assert [a.kind for a in actions] == [ActionKind.OPEN]
    assert actions[0].executable


def test_just_past_the_grace_boundary_re_baselines() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(5.001), grace_seconds=5.0)
    assert [a.skip_reason for a in actions] == [SkipReason.PREEXISTING]


def test_zero_grace_means_any_disconnection_excludes() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([], [link(connected=False)], now=clock.advance(0.2))
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(0.2), grace_seconds=0.0)
    assert [a.skip_reason for a in actions] == [SkipReason.PREEXISTING]


def test_a_master_outage_also_breaks_continuity() -> None:
    """The master's link dropping means no tick runs at all, so the gap is the same gap --
    positions the master opened while we were blind must not be adopted."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)

    # no calls for two minutes: the engine skipped reconciliation because the master was down
    actions = ledger.step([pos(ticket=5)], [link()], now=clock.advance(120.0), grace_seconds=5.0)
    assert [a.skip_reason for a in actions] == [SkipReason.PREEXISTING]


def test_a_copy_made_before_an_outage_is_still_mirrored_after_it() -> None:
    """Re-baselining must not orphan a real position. It excludes what we do not own; what we
    do own stays fully tracked, including its close."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))
    assert len(ledger.believed()) == 1

    ledger.step([pos(ticket=5)], [link(connected=False)], now=clock.advance(600.0))
    resumed = ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))
    assert resumed == [], "still ours, nothing changed"

    closed = ledger.step([], [link()], now=clock.advance(1.0))
    assert [a.kind for a in closed] == [ActionKind.CLOSE]


def test_exclusions_do_not_grow_without_bound() -> None:
    clock = Clock()
    ledger = CopyLedger()
    ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))
    assert ledger._excluded[TID_SLAVE] == {5}
    ledger.step([], [link()], now=clock.advance(1.0))
    assert ledger._excluded[TID_SLAVE] == set()


# --- persistence ----------------------------------------------------------------------------


def test_a_restart_proposes_nothing_for_what_it_already_tracked(tmp_path: Path) -> None:
    """The Stage 2a exit criterion, now carried by the store rather than a JSON file."""
    clock = Clock()
    path = tmp_path / "beliefs.db"
    master = [pos(ticket=5), pos(ticket=6, symbol="GBPUSD")]

    with MappingStore(path) as store:
        ledger = CopyLedger(store)
        watching(ledger, clock)
        assert len(ledger.step(master, [link()], now=clock.advance(1.0))) == 2

    with MappingStore(path) as store:
        revived = CopyLedger(store)
        assert revived.step(master, [link()], now=clock.advance(1.0)) == []
        assert len(revived.believed()) == 2


def test_a_restart_does_not_adopt_what_it_never_copied() -> None:
    clock = Clock()
    revived = CopyLedger(MappingStore())
    actions = revived.step([pos(ticket=5)], [link()], now=clock.advance(1.0))
    assert [a.skip_reason for a in actions] == [SkipReason.PREEXISTING]


def test_a_restarted_ledger_still_sees_what_changed_while_it_was_down(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "beliefs.db"
    with MappingStore(path) as store:
        ledger = CopyLedger(store)
        watching(ledger, clock)
        ledger.step([pos(ticket=5, volume=1.0)], [link()], now=clock.advance(1.0))

    with MappingStore(path) as store:
        revived = CopyLedger(store)
        actions = revived.step([pos(ticket=5, volume=0.5)], [link()], now=clock.advance(1.0))
        assert [a.kind for a in actions] == [ActionKind.REDUCE]


def test_the_two_stores_behave_identically(tmp_path: Path) -> None:
    """The dry run is only worth running if it exercises the live code path. If these two ever
    diverge, the rehearsal stops meaning anything."""
    master = [pos(ticket=5, volume=1.0)]
    outcomes = []
    for store in (None, MappingStore(tmp_path / "b.db")):
        clock = Clock()
        ledger = CopyLedger(store)
        watching(ledger, clock)
        first = ledger.step(master, [link()], now=clock.advance(1.0))
        second = ledger.step(master, [link()], now=clock.advance(1.0))
        reduced = ledger.step([pos(ticket=5, volume=0.5)], [link()], now=clock.advance(1.0))
        outcomes.append(([a.kind for a in first], second, [a.close_fraction for a in reduced]))
    assert outcomes[0] == outcomes[1]


# --- engine guards --------------------------------------------------------------------------


@pytest.fixture
def engine_cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        copying_enabled=True,
        terminals={
            TID_MASTER: TerminalConfig(
                terminal_id=TID_MASTER, label="Master", exe_path=exe, role=Role.MASTER
            ),
            TID_SLAVE: TerminalConfig(
                terminal_id=TID_SLAVE, label="Slave", exe_path=exe, role=Role.SLAVE
            ),
        },
    )


@pytest.fixture
def engine(engine_cfg: AppConfig, tmp_path: Path) -> Engine:
    """An engine that is never started, so no worker process is spawned."""
    return Engine(engine_cfg, state_path=tmp_path / "dryrun.json")


def snap(
    terminal_id: str = TID_MASTER,
    state: WorkerState = WorkerState.CONNECTED,
    open_positions: tuple[Position, ...] | None = (),
    pending_orders: tuple[PendingOrder, ...] | None = (),
) -> Snapshot:
    return Snapshot(
        terminal_id=terminal_id,
        state=state,
        open_positions=open_positions,
        pending_orders=pending_orders,
    )


def wire_slave(eng: Engine, state: WorkerState = WorkerState.CONNECTED, age_s: float = 0.0) -> None:
    """Give the engine a slave snapshot, as the drain thread would have."""
    eng._snapshots[TID_SLAVE] = snap(terminal_id=TID_SLAVE, state=state)
    eng._received_at[TID_SLAVE] = time.monotonic() - age_s


def watch(eng: Engine) -> None:
    """One tick against a flat master, establishing continuity."""
    eng._reconcile(snap(open_positions=()))


def test_engine_records_intents_from_the_master(engine: Engine) -> None:
    wire_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    recorded = engine.recent_actions()
    assert [r.action.kind for r in recorded] == [ActionKind.OPEN]
    assert recorded[0].action.executable
    assert engine.believed_copies() == 1


def test_engine_reports_but_never_copies_what_was_already_open(engine: Engine) -> None:
    wire_slave(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.recent_actions()[-1].action.skip_reason is SkipReason.PREEXISTING
    assert engine.believed_copies() == 0


def test_engine_treats_a_slave_with_no_snapshot_as_disconnected(engine: Engine) -> None:
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.recent_actions() == []


def test_engine_treats_a_disconnected_slave_as_not_copyable(engine: Engine) -> None:
    wire_slave(engine, state=WorkerState.DISCONNECTED)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.recent_actions() == []


def test_engine_will_not_copy_into_a_stale_slave(engine: Engine) -> None:
    """CONNECTED but silent is the trap: the last snapshot looked fine, and trusting it would
    make a wedged worker copyable forever."""
    wire_slave(engine, age_s=engine.config.heartbeat_timeout_s + 1.0)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.recent_actions() == []


def test_engine_ignores_snapshots_from_slaves(engine: Engine) -> None:
    wire_slave(engine)
    engine._reconcile(snap(terminal_id=TID_SLAVE, open_positions=(pos(),)))
    assert engine.recent_actions() == []


def test_engine_does_nothing_while_detection_is_off(engine: Engine) -> None:
    wire_slave(engine)
    engine.config.copying_enabled = False
    engine._reconcile(snap(open_positions=(pos(),)))
    assert engine.recent_actions() == []


def test_engine_will_not_reconcile_a_disconnected_master(engine: Engine) -> None:
    wire_slave(engine)
    engine._reconcile(snap(state=WorkerState.DISCONNECTED, open_positions=(pos(),)))
    assert engine.recent_actions() == []


def test_a_failed_position_read_never_looks_like_a_flat_master(engine: Engine) -> None:
    """The expensive mistake available in this whole stage: reading None as "nothing open"
    would propose closing every copied position at once."""
    wire_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.believed_copies() == 1
    before = engine.action_total

    engine._reconcile(snap(open_positions=None))
    assert engine.action_total == before, "no CLOSE derived from an unreadable tick"
    assert engine.believed_copies() == 1


def test_an_empty_position_list_does_mean_flat(engine: Engine) -> None:
    wire_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    engine._reconcile(snap(open_positions=()))
    assert engine.recent_actions()[-1].action.kind is ActionKind.CLOSE
    assert engine.believed_copies() == 0


def test_an_unreadable_order_list_pauses_detection_loudly(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A silent pause is indistinguishable from an idle master -- which is how "deleting a
    pending order does not reflect" looked from the outside."""
    wire_slave(engine)
    watch(engine)
    with caplog.at_level("WARNING"):
        engine._reconcile(snap(open_positions=(pos(),), pending_orders=None))
    assert engine.blocked_reason is not None
    assert any("detection paused" in r.getMessage() for r in caplog.records)


def test_the_pause_is_announced_once_not_every_tick(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    wire_slave(engine)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            engine._reconcile(snap(pending_orders=None))
    assert len([r for r in caplog.records if "detection paused" in r.getMessage()]) == 1


def test_detection_resumes_and_says_so(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    wire_slave(engine)
    engine._reconcile(snap(pending_orders=None))
    with caplog.at_level("INFO"):
        engine._reconcile(snap(pending_orders=()))
    assert engine.blocked_reason is None
    assert any("detection resumed" in r.getMessage() for r in caplog.records)


def test_an_order_deleted_on_the_master_is_cancelled(engine: Engine) -> None:
    """End to end through the engine, which is where the dry run said nothing happened."""
    engine.config.terminals[TID_SLAVE].copy_pending = True
    wire_slave(engine)
    watch(engine)

    engine._reconcile(snap(pending_orders=(order(ticket=50),)))
    assert [r.action.kind for r in engine.recent_actions()] == [ActionKind.PLACE]

    engine._reconcile(snap(pending_orders=()))
    assert engine.recent_actions()[-1].action.kind is ActionKind.CANCEL


def test_engine_refuses_to_act_on_a_dry_run_it_cannot_honour(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Stage 2a has no execution code, so dry_run=False must be loudly declined, not obeyed
    and not silently ignored."""
    wire_slave(engine)
    engine.config.dry_run = False
    with caplog.at_level("ERROR"):
        engine._reconcile(snap(open_positions=(pos(),)))
    assert any("sending nothing" in r.getMessage() for r in caplog.records)


def test_engine_persists_and_restores_its_ledger(engine_cfg: AppConfig, tmp_path: Path) -> None:
    state = tmp_path / "dryrun.db"
    first = Engine(engine_cfg, state_path=state)
    wire_slave(first)
    watch(first)
    first._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert first.believed_copies() == 1
    assert state.exists()

    second = Engine(engine_cfg, state_path=state)
    wire_slave(second)
    second._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert second.recent_actions() == [], "a restart re-proposes nothing"
    assert second.believed_copies() == 1


def test_a_dry_run_and_a_live_session_do_not_share_beliefs(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """A simulated position recorded in the live table would later be indistinguishable from
    one that actually exists, and the copier would try to close something never opened."""
    eng = Engine(
        engine_cfg, state_path=tmp_path / "dryrun.db", mapping_path=tmp_path / "mapping.db"
    )
    wire_slave(eng)
    watch(eng)
    eng._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert eng.believed_copies() == 1

    eng.config.dry_run = False
    eng._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert eng.believed_copies() == 0, "the live store starts empty"

    eng.config.dry_run = True
    eng._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert eng.believed_copies() == 1, "and the simulated belief is still there"


def test_clearing_the_ledger_forgets_every_belief(engine: Engine) -> None:
    wire_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    engine.clear_detection_state()
    assert engine.believed_copies() == 0
    assert engine.recent_actions() == []
