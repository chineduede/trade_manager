"""Diff engine tests.

The point of Stage 2a is that this layer is provable without a terminal, a broker or a network,
so every test here is pure data in and data out. If the copier's decisions are wrong, they are
wrong here, and they cost nothing to get wrong here.

The engine-level tests at the bottom instantiate an :class:`Engine` but never start it, so no
worker is spawned; they exercise only the guards that decide whether reconciliation may run at
all.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from mt5copier.config import AppConfig, NoStopPolicy, Role, SizingMode, TerminalConfig
from mt5copier.engine import Engine
from mt5copier.protocol import (
    COPIER_MAGIC,
    OrderCommand,
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
from mt5copier.store import Copy, CopyKind, MappingStore

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
    magic: int = 0,
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
        magic=magic,
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


def test_an_explicit_mapping_overrides_the_decoration_in_the_logged_symbol() -> None:
    """The log and the executor must not disagree about which instrument a copy is on, so
    map_symbol shares its precedence with the real resolver rather than re-implementing it."""
    cfg = slave(symbol_suffix="p", symbol_map={"XAUUSD": "XAUUSD.s"})
    assert map_symbol("XAUUSD", cfg) == "XAUUSD.s"
    assert map_symbol("EURGBP", cfg) == "EURGBPp", "unmapped symbols still take the suffix"
    a = reconcile_slave([pos(symbol="XAUUSD")], [], cfg)[0]
    assert a.slave_symbol == "XAUUSD.s"


def test_a_dry_run_believes_the_mapped_name() -> None:
    """_simulated_result turns slave_symbol into durable belief, so an approximation that
    ignored the map would make the dry run rehearse the wrong instrument."""
    clock = Clock()
    ledger = CopyLedger()
    mapped = link(symbol_suffix="p", symbol_map={"XAUUSD": "XAUUSD.s"})
    watching(ledger, clock, [mapped])
    ledger.step([pos(ticket=7, symbol="XAUUSD")], [mapped], now=clock.advance(1.0))
    assert ledger.believed()[0].slave_symbol == "XAUUSD.s"


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


def test_clearing_the_masters_stop_is_mirrored_and_flagged() -> None:
    """Mirroring is the contract -- the alternative is a copy that exits where the master
    does not -- but disarming a protected copy must not be inferable only from `sl 0`."""
    actions = reconcile_slave([pos(sl=0.0)], [copied(sl=1.08)], slave())
    assert [a.kind for a in actions] == [ActionKind.MODIFY_SLTP]
    assert actions[0].sl == 0.0
    assert actions[0].unprotected
    assert "UNPROTECTED: removes the slave's stop" in actions[0].describe()


def test_an_inverted_slave_is_disarmed_by_the_master_removing_its_target() -> None:
    """effective_levels swaps the pair, so on a reversed copy it is the take-profit that is
    the stop. Surprising enough that the flag is the only thing making it visible."""
    actions = reconcile_slave(
        [pos(sl=1.08, tp=0.0)], [copied(sl=1.09, tp=1.08)], slave(invert=True)
    )
    assert [a.kind for a in actions] == [ActionKind.MODIFY_SLTP]
    assert actions[0].sl == 0.0, "the master's absent target is the reversed copy's stop"
    assert actions[0].unprotected


def test_a_skipped_stop_removal_is_not_flagged_as_unprotected() -> None:
    """An action that will not be sent disarms nothing."""
    actions = reconcile_slave([pos(sl=0.0)], [copied(sl=1.08)], slave(copy_sltp=False))
    assert actions[0].skip_reason is SkipReason.SLTP_SYNC_DISABLED
    assert not actions[0].unprotected


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

    # The position carries the order's own stop, which is what MT5 does on a fill -- the
    # helpers' defaults differ, and a mismatch here would be a drift to correct, not a
    # handover to observe. test_a_fill_keeps_the_orders_levels_so_drift_is_corrected covers
    # that case deliberately.
    landed = pos(ticket=50, sl=1.07500)
    filled = ledger.step([landed], [pending_link()], master_orders=[], now=clock.advance(1.0))
    assert [a.kind for a in filled] == [ActionKind.ORDER_FILLED]
    assert ledger.believed_orders() == []
    assert [c.master_ticket for c in ledger.believed()] == [50], "now tracked as a position"

    assert ledger.step([landed], [pending_link()], now=clock.advance(1.0)) == []
    closed = ledger.step([], [pending_link()], now=clock.advance(1.0))
    assert [a.kind for a in closed] == [ActionKind.CLOSE]


def test_a_fill_keeps_the_orders_levels_so_drift_is_corrected() -> None:
    """Belief must record what the *slave* carries, not what the master holds.

    The slave's order fills with whatever levels it was resting on, which is the order's
    believed pair -- and if a MODIFY_ORDER was refused before the fill, that is not the
    master's pair. Adopting the master's here would write the drift away as already fixed,
    and since the diff compares the master against belief and never against the slave,
    nothing would look again. Keeping the order's makes the very next tick correct it.
    """
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock, [pending_link()])
    ledger.step(
        [], [pending_link()], master_orders=[order(ticket=50, sl=1.07500)], now=clock.advance(1.0)
    )

    # Live, so the sync this provokes stays in flight and the handed-over value is still
    # visible; simulating would apply it in the same tick and hide what was written.
    filled = ledger.step(
        [pos(ticket=50, sl=1.08000)],
        [pending_link()],
        master_orders=[],
        now=clock.advance(1.0),
        simulate=False,
    )
    assert ActionKind.ORDER_FILLED in [a.kind for a in filled]
    assert ledger.believed()[0].synced_sl == pytest.approx(1.07500), "what the slave rests on"

    sync = [a for a in filled if a.kind is ActionKind.MODIFY_SLTP]
    assert len(sync) == 1, "the gap is noticed rather than written away"
    assert sync[0].sl == pytest.approx(1.08000)


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
    balance: float | None = 10_000.0,
) -> Snapshot:
    return Snapshot(
        terminal_id=terminal_id,
        state=state,
        open_positions=open_positions,
        pending_orders=pending_orders,
        balance=balance,
    )


def wire_slave(
    eng: Engine,
    state: WorkerState = WorkerState.CONNECTED,
    age_s: float = 0.0,
    terminal_id: str = TID_SLAVE,
    open_positions: tuple[Position, ...] | None = (),
    pending_orders: tuple[PendingOrder, ...] | None = (),
    balance: float | None = 10_000.0,
) -> None:
    """Give the engine a slave snapshot, as the drain thread would have.

    The balance is not incidental: a percent-of-balance slave whose balance cannot be read
    cannot be sized, and the engine holds it rather than guessing.
    """
    eng._snapshots[terminal_id] = snap(
        terminal_id=terminal_id,
        state=state,
        open_positions=open_positions,
        pending_orders=pending_orders,
        balance=balance,
    )
    eng._received_at[terminal_id] = time.monotonic() - age_s


class _FakeProcess:
    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        pass


class _FakeQueue:
    """Records what the engine tried to send, instead of crossing a process boundary."""

    def __init__(self, accept: bool = True) -> None:
        self.sent: list[object] = []
        self.accept = accept

    def put_nowait(self, item: object) -> None:
        if not self.accept:
            raise queue.Full
        self.sent.append(item)

    def close(self) -> None:
        pass


class _FakeWorker:
    def __init__(self, terminal_id: str, accept: bool = True) -> None:
        self.terminal_id = terminal_id
        self.process = _FakeProcess()
        self.cmd_q = _FakeQueue(accept=accept)
        self.started_at = time.monotonic()


def arm_slave(eng: Engine, terminal_id: str = TID_SLAVE, accept: bool = True) -> _FakeQueue:
    """Give the engine somewhere to send commands, without spawning a process."""
    worker = _FakeWorker(terminal_id, accept=accept)
    eng._workers[terminal_id] = worker  # type: ignore[assignment]
    return worker.cmd_q


def go_live(eng: Engine) -> None:
    eng.config.dry_run = False


def answer(
    eng: Engine,
    cmd: OrderCommand,
    *,
    ok: bool = True,
    retryable: bool = False,
    slave_ticket: int = 5001,
    command_id: int | None = None,
) -> None:
    """Deliver the result the worker would have published for this command."""
    eng._on_result(
        OrderResult(
            command_id=cmd.command_id if command_id is None else command_id,
            terminal_id=TID_SLAVE,
            op=cmd.op,
            master_ticket=cmd.master_ticket,
            ok=ok,
            retryable=retryable,
            slave_ticket=slave_ticket,
            slave_symbol=cmd.master_symbol,
            requested_volume=0.5,
            filled_volume=0.5,
        )
    )


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


def test_a_dry_run_dispatches_nothing(engine: Engine) -> None:
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert [r.action.kind for r in engine.recent_actions()] == [ActionKind.OPEN]
    assert cmd_q.sent == [], "a rehearsal sends nothing, however complete it looks"


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


# --- startup reconciliation -----------------------------------------------------------------
#
# The store and the terminal can only disagree while the copier is not running, and they
# disagree in two directions: a record whose position is gone, and a position of ours with no
# record. The first derives a close against nothing; the second leaves real money untracked.


def live_engine(cfg: AppConfig, tmp_path: Path) -> Engine:
    """An engine in live mode, so the stale-record sweep actually runs."""
    eng = Engine(cfg, state_path=tmp_path / "dryrun.db", mapping_path=tmp_path / "mapping.db")
    eng.config.dry_run = False
    return eng


def seed_live_store(tmp_path: Path, copy: Copy) -> None:
    """Put a row in the live mapping table before the engine opens it."""
    with MappingStore(tmp_path / "mapping.db") as store:
        store.record(copy)


def stale_row(master_ticket: int = 11, slave_ticket: int = 900) -> Copy:
    return Copy(
        terminal_id=TID_SLAVE,
        master_ticket=master_ticket,
        kind=CopyKind.POSITION,
        slave_symbol="EURUSD",
        direction=PositionType.BUY,
        master_volume=1.0,
        slave_ticket=slave_ticket,
        slave_volume=0.5,
    )


def test_a_slave_is_not_copied_to_until_it_has_been_reconciled(engine: Engine) -> None:
    """An unreadable position list means reconciliation cannot run, and a slave that has not
    been reconciled must not be copied to -- we do not know what it is already holding."""
    wire_slave(engine, open_positions=None)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert engine.recent_actions() == []
    assert "cannot be read" in engine.holds()[TID_SLAVE]


def test_startup_reconciliation_drops_a_record_whose_slave_position_is_gone(
    engine_cfg: AppConfig, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Stopped out, or closed by hand, while we were not running. There is nothing left to
    close, so the record must go rather than produce a CLOSE against thin air."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    wire_slave(eng, open_positions=())

    with caplog.at_level("WARNING"):
        eng._reconcile(snap(open_positions=()))

    assert eng.believed_copies() == 0
    assert [r.action.kind for r in eng.recent_actions()] == []
    assert any("stale record" in r.getMessage() for r in caplog.records)


def test_a_forgotten_record_does_not_become_a_fresh_open(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """The ordering guarantee. The row is dropped first, so the baseline that follows sees the
    master's position as unbelieved and excludes it -- a trade closed by hand during downtime
    is neither closed again nor re-opened."""
    seed_live_store(tmp_path, stale_row(master_ticket=11))
    eng = live_engine(engine_cfg, tmp_path)
    wire_slave(eng, open_positions=())

    eng._reconcile(snap(open_positions=(pos(ticket=11),)))

    opens = [
        r for r in eng.recent_actions() if r.action.executable and r.action.kind is ActionKind.OPEN
    ]
    assert opens == [], "the position was already open; dropping our record does not make it new"
    assert eng.recent_actions()[-1].action.skip_reason is SkipReason.PREEXISTING
    assert eng.believed_copies() == 0


def test_an_orphan_holds_that_slave_and_says_so(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A position carrying our magic with no record is one we opened and never recorded.
    Nobody is tracking it, so the master closing will not close it."""
    wire_slave(engine, open_positions=(pos(ticket=999, magic=COPIER_MAGIC),))
    with caplog.at_level("ERROR"):
        watch(engine)
        engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert engine.recent_actions() == []
    assert [o.slave_ticket for o in engine.orphans()] == [999]
    assert any("orphan" in r.getMessage() for r in caplog.records)


def test_an_orphan_pending_order_holds_the_slave_too(engine: Engine) -> None:
    """It has not moved money yet, but it will the moment it fills, with nothing tracking it."""
    wire_slave(
        engine,
        pending_orders=(
            PendingOrder(
                ticket=888,
                symbol="EURUSD",
                type=OrderType.BUY_LIMIT,
                volume=1.0,
                price_open=1.07,
                magic=COPIER_MAGIC,
            ),
        ),
    )
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert engine.recent_actions() == []
    assert [o.slave_ticket for o in engine.orphans()] == [888]


def test_the_users_own_position_on_a_slave_is_not_an_orphan(engine: Engine) -> None:
    """Anything without our magic is their trading and none of our business."""
    wire_slave(engine, open_positions=(pos(ticket=999, magic=0),))
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert engine.orphans() == []
    assert engine.holds() == {}
    assert [r.action.kind for r in engine.recent_actions()] == [ActionKind.OPEN]


def test_other_slaves_keep_copying_while_one_is_held(engine_cfg: AppConfig, tmp_path: Path) -> None:
    """A hold is one slave's problem. Fifteen terminals must not stop because one has an
    unexplained position."""
    exe = tmp_path / "terminal64.exe"
    engine_cfg.terminals[TID_SLAVE2] = TerminalConfig(
        terminal_id=TID_SLAVE2, label="Slave2", exe_path=exe, role=Role.SLAVE
    )
    eng = Engine(engine_cfg, state_path=tmp_path / "dryrun.db")
    wire_slave(eng, open_positions=(pos(ticket=999, magic=COPIER_MAGIC),))
    wire_slave(eng, terminal_id=TID_SLAVE2)

    watch(eng)
    eng._reconcile(snap(open_positions=(pos(ticket=11),)))

    copied_to = {r.action.terminal_id for r in eng.recent_actions()}
    assert copied_to == {TID_SLAVE2}
    assert set(eng.holds()) == {TID_SLAVE}


def test_acknowledging_releases_the_hold_and_re_baselines(engine: Engine) -> None:
    """Acknowledging means "I have dealt with these", not "copy as if nothing happened". While
    held we were not copying, so whatever the master opened meanwhile is not ours to adopt."""
    wire_slave(engine, open_positions=(pos(ticket=999, magic=COPIER_MAGIC),))
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.recent_actions() == []

    assert engine.acknowledge_orphans(TID_SLAVE) == 1
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert engine.holds() == {}
    assert engine.recent_actions()[-1].action.skip_reason is SkipReason.PREEXISTING
    assert engine.believed_copies() == 0


def test_acknowledging_keeps_the_orphans_visible(engine: Engine) -> None:
    """They are still real, still open and still untracked. Erasing the only record of them
    the moment the user clicks is the silent loss this mechanism exists to prevent."""
    wire_slave(engine, open_positions=(pos(ticket=999, magic=COPIER_MAGIC),))
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    engine.acknowledge_orphans(TID_SLAVE)

    assert [o.slave_ticket for o in engine.orphans()] == [999]


def test_acknowledging_nothing_is_harmless(engine: Engine) -> None:
    assert engine.acknowledge_orphans(TID_SLAVE) == 0


def test_a_close_during_a_hold_is_mirrored_once_it_lifts(engine: Engine) -> None:
    """Deferred, not lost. The belief survives the hold, so the close is derived as soon as the
    slave is copyable again. This is the one place the hold costs money: it rides the move."""
    wire_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.believed_copies() == 1

    wire_slave(engine, balance=None)
    engine._reconcile(snap(open_positions=()))
    assert [r.action.kind for r in engine.recent_actions()] == [ActionKind.OPEN], "held"

    wire_slave(engine, balance=10_000.0)
    engine._reconcile(snap(open_positions=()))
    assert engine.recent_actions()[-1].action.kind is ActionKind.CLOSE


def test_a_slave_with_no_readable_balance_is_not_copied_to(engine: Engine) -> None:
    """A zero budget is not a safe fallback: the executor reports it as a failed order, and a
    failed open is final. Refusing to derive anything is the only non-destructive answer."""
    wire_slave(engine, balance=None)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert engine.recent_actions() == []
    assert "balance" in engine.holds()[TID_SLAVE]


def test_a_fixed_cash_slave_copies_without_a_balance(engine_cfg: AppConfig, tmp_path: Path) -> None:
    """Fixed-cash sizing never reads the balance, so an unreadable one need not stop it."""
    engine_cfg.terminals[TID_SLAVE].sizing_mode = SizingMode.FIXED_CASH_RISK
    eng = Engine(engine_cfg, state_path=tmp_path / "dryrun.db")
    wire_slave(eng, balance=None)
    watch(eng)
    eng._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert [r.action.kind for r in eng.recent_actions()] == [ActionKind.OPEN]


def test_a_dry_run_does_not_drop_its_simulated_beliefs(engine: Engine) -> None:
    """Simulated beliefs carry a negated master ticket as their slave ticket, so every one of
    them looks missing against a real position list. Sweeping stale rows in a dry run would
    delete the whole session's state on its first tick."""
    wire_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.believed_copies() == 1

    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.believed_copies() == 1, "the simulated belief survived reconciliation"


def test_the_hold_is_announced_once_not_every_tick(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    wire_slave(engine, balance=None)
    with caplog.at_level("WARNING"):
        watch(engine)
        engine._reconcile(snap(open_positions=(pos(ticket=11),)))
        engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    held = [r for r in caplog.records if "held" in r.getMessage()]
    assert len(held) == 1, "announce the transition, not every tick"


def test_lifting_a_hold_is_announced(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """A copier that goes quiet and then quietly resumes is indistinguishable from one that
    never stopped, so the recovery is said out loud too."""
    wire_slave(engine, balance=None)
    watch(engine)
    with caplog.at_level("INFO"):
        wire_slave(engine, balance=10_000.0)
        engine._reconcile(snap(open_positions=()))

    assert any("resumed" in r.getMessage() for r in caplog.records)


def test_a_restart_reconciles_before_deriving_anything(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """The whole point, end to end: a live record whose position vanished during downtime is
    dropped on the first tick, and no close is ever proposed for it."""
    seed_live_store(tmp_path, stale_row(master_ticket=11, slave_ticket=900))
    eng = live_engine(engine_cfg, tmp_path)
    wire_slave(eng, open_positions=(pos(ticket=555, magic=0),))

    eng._reconcile(snap(open_positions=()))

    assert eng.orphans() == [], "the user's own trade is not ours"
    assert eng.holds() == {}
    assert eng.believed_copies() == 0
    assert all(r.action.kind is not ActionKind.CLOSE for r in eng.recent_actions())


# --- dispatch -------------------------------------------------------------------------------
#
# From here on an order can actually leave the process, so these tests are about what is sent,
# what is deliberately not sent, and what is believed only once the broker has answered.


def test_an_executable_action_becomes_an_order_command(engine: Engine) -> None:
    """The engine decides what and how much money; the worker decides how many lots. So every
    per-slave setting the sizing needs has to be carried in the command itself."""
    cfg = engine.config.terminals[TID_SLAVE]
    cfg.symbol_prefix = "x"
    cfg.symbol_map = {"EURUSD": "EURUSD.s"}
    cfg.max_lot = 0.02
    cfg.commission_per_lot_round_turn = 7.0
    cfg.slippage_buffer_points = 3
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)

    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert len(cmd_q.sent) == 1
    cmd = cmd_q.sent[0]
    assert isinstance(cmd, OrderCommand)
    assert cmd.op is ActionKind.OPEN
    assert cmd.master_ticket == 11
    assert cmd.magic == COPIER_MAGIC
    assert cmd.risk_amount == pytest.approx(100.0), "1% of a 10,000 balance"
    assert cmd.allow_live is False, "the demo-only guard is not bypassed by default"
    assert cmd.symbol_prefix == "x"
    assert cmd.symbol_map == (("EURUSD", "EURUSD.s"),), (
        "sorted pairs, not a dict -- OrderCommand is frozen, and the worker reads no config"
    )
    assert cmd.max_lot == pytest.approx(0.02)
    assert cmd.commission_per_lot == pytest.approx(7.0)
    assert cmd.slippage_points == 3
    assert cmd.deviation_points == cfg.max_deviation_points


def test_nothing_is_believed_until_the_broker_answers(engine: Engine) -> None:
    """The hazard the in-flight mechanism exists for: between sending an open and learning it
    filled, the master's state has not changed, so the same open derives again."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert engine.believed_copies() == 0, "not yet -- nothing has filled"

    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert len(cmd_q.sent) == 1, "withheld while the first command is unanswered"

    answer(engine, cmd_q.sent[0])
    assert engine.believed_copies() == 1
    assert [r.result.ok for r in engine.recent_results()] == [True]


def test_a_skipped_action_is_never_dispatched(engine: Engine) -> None:
    """A trade with no stop, on a slave set to SKIP, is recorded and sent nowhere."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=0.0),)))

    assert engine.recent_actions()[-1].action.skip_reason is not None
    assert cmd_q.sent == []


def test_a_stop_move_is_dispatched_and_belief_follows_the_result(engine: Engine) -> None:
    """Stage 3. Belief must move when the broker confirms, and not a moment before."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    answer(engine, cmd_q.sent[0])

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))

    sync = cmd_q.sent[-1]
    assert isinstance(sync, OrderCommand)
    assert sync.op is ActionKind.MODIFY_SLTP
    assert sync.sl == pytest.approx(1.0825)
    assert sync.slave_ticket == 5001, "it acts on the copy, not on the master's ticket"
    assert engine._ledger.believed(TID_SLAVE)[0].synced_sl == pytest.approx(1.08), (
        "still the old stop -- nothing is believed until the broker answers"
    )

    answer(engine, sync)
    assert engine._ledger.believed(TID_SLAVE)[0].synced_sl == pytest.approx(1.0825)


def test_a_second_stop_move_waits_for_the_first_to_be_answered(engine: Engine) -> None:
    """One command per key in flight. Two moves in quick succession must not race each other
    onto the terminal, and the later value must not be lost when the earlier one lands."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    answer(engine, cmd_q.sent[0])

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))
    first = cmd_q.sent[-1]
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0850),)))
    assert cmd_q.sent[-1] is first, "withheld while the first is unanswered"

    answer(engine, first)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0850),)))
    assert cmd_q.sent[-1].sl == pytest.approx(1.0850), "the newer value is not dropped"


def test_a_refused_stop_move_is_not_sent_again_at_the_same_value(engine: Engine) -> None:
    """A broker refusal is not retried: the identical request would meet the identical
    refusal at every poll. It is surfaced once, and a *new* master value re-arms it."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    answer(engine, cmd_q.sent[0])

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))
    answer(engine, cmd_q.sent[-1], ok=False)
    sent = len(cmd_q.sent)

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))
    assert len(cmd_q.sent) == sent, "not re-sent at the value that was refused"

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0860),)))
    assert len(cmd_q.sent) == sent + 1, "but a new stop is a new decision"


def test_a_transient_failure_on_a_stop_move_is_derived_again(engine: Engine) -> None:
    """A stop should still follow the master a tick later; a target state does not go stale
    the way an entry does. Without re-arming, the dedup memory suppresses the identical
    action and the copy silently keeps the old stop for the rest of the session."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    answer(engine, cmd_q.sent[0])

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))
    answer(engine, cmd_q.sent[-1], ok=False, retryable=True)
    sent = len(cmd_q.sent)

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))
    assert len(cmd_q.sent) == sent + 1, "the same stop is asked for again"
    assert cmd_q.sent[-1].sl == pytest.approx(1.0825)


def test_a_transient_failure_on_a_partial_close_is_not_derived_again(engine: Engine) -> None:
    """The exception among the target-state ops, and the dangerous one. A REDUCE closes a
    *fraction*: if the first one reached the broker and only its answer was lost, deriving it
    again closes another slice, and over-closing cannot be undone."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, volume=1.0),)))
    answer(engine, cmd_q.sent[0])

    engine._reconcile(snap(open_positions=(pos(ticket=11, volume=0.5),)))
    reduce_cmd = cmd_q.sent[-1]
    assert reduce_cmd.op is ActionKind.REDUCE
    answer(engine, reduce_cmd, ok=False, retryable=True)
    sent = len(cmd_q.sent)

    engine._reconcile(snap(open_positions=(pos(ticket=11, volume=0.5),)))
    assert len(cmd_q.sent) == sent, "left alone rather than reduced twice"


def test_a_stop_move_onto_a_vanished_copy_does_not_become_belief(engine: Engine) -> None:
    """Reported ok because there was nothing left to get wrong, not because the levels
    landed. Recording them would describe a stop the slave never carried, and the diff
    compares the master against belief and never against the slave."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    answer(engine, cmd_q.sent[0])

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))
    sync = cmd_q.sent[-1]
    engine._on_result(
        OrderResult(
            command_id=sync.command_id,
            terminal_id=TID_SLAVE,
            op=sync.op,
            master_ticket=sync.master_ticket,
            ok=True,
            target_gone=True,
            outcome="the target is gone",
        )
    )
    assert engine._ledger.believed(TID_SLAVE)[0].synced_sl == pytest.approx(1.08)


def test_a_transient_failure_on_an_open_is_still_final(engine: Engine) -> None:
    """The exception to the rule above, and the reason it is a rule about target states: by
    the time a link comes back, an entry would be filling far from the master's."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    answer(engine, cmd_q.sent[0], ok=False, retryable=True)
    sent = len(cmd_q.sent)

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    assert len(cmd_q.sent) == sent, "excluded, not retried"


def test_moving_a_resting_order_is_dispatched_and_belief_follows(engine: Engine) -> None:
    """The order half of Stage 3. Its entry price moves, which a position's cannot."""
    engine.config.terminals[TID_SLAVE].copy_pending = True
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(), pending_orders=(order(ticket=50, price_open=1.08),)))
    placed = cmd_q.sent[-1]
    assert placed.op is ActionKind.PLACE
    answer(engine, placed)

    engine._reconcile(snap(open_positions=(), pending_orders=(order(ticket=50, price_open=1.079),)))
    moved = cmd_q.sent[-1]
    assert moved.op is ActionKind.MODIFY_ORDER
    assert moved.entry == pytest.approx(1.079)
    assert moved.slave_ticket == 5001
    assert engine._ledger.believed_orders(TID_SLAVE)[0].entry == pytest.approx(1.08), (
        "not believed until the broker answers"
    )

    answer(engine, moved)
    assert engine._ledger.believed_orders(TID_SLAVE)[0].entry == pytest.approx(1.079)


def test_sltp_sync_off_derives_the_action_and_sends_nothing(engine: Engine) -> None:
    """copy_sltp is the switch, and a skip is a record rather than an instruction."""
    engine.config.terminals[TID_SLAVE].copy_sltp = False
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    answer(engine, cmd_q.sent[0])
    sent = len(cmd_q.sent)

    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.0825),)))

    latest = engine.recent_actions()[-1].action
    assert latest.kind is ActionKind.MODIFY_SLTP
    assert latest.skip_reason is SkipReason.SLTP_SYNC_DISABLED
    assert len(cmd_q.sent) == sent


def test_order_filled_is_never_dispatched(engine: Engine) -> None:
    """It is a report about the slave's own fill, not an instruction. Nothing to send."""
    engine.config.terminals[TID_SLAVE].copy_pending = True
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)

    engine._reconcile(
        snap(pending_orders=(order(ticket=11),), open_positions=()),
    )
    placed = [c for c in cmd_q.sent if isinstance(c, OrderCommand)]
    if placed:
        answer(engine, placed[0])

    # The master's order fills: it leaves the order list and appears as a position.
    engine._reconcile(snap(open_positions=(pos(ticket=11),), pending_orders=()))

    assert all(
        c.op is not ActionKind.ORDER_FILLED for c in cmd_q.sent if isinstance(c, OrderCommand)
    )


def test_a_close_is_dispatched_and_forgets_the_row(engine: Engine) -> None:
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    answer(engine, cmd_q.sent[0])

    engine._reconcile(snap(open_positions=()))
    close = cmd_q.sent[-1]
    assert isinstance(close, OrderCommand)
    assert close.op is ActionKind.CLOSE
    assert close.slave_ticket == 5001, "the real ticket the broker gave us"

    answer(engine, close)
    assert engine.believed_copies() == 0


def test_a_result_answering_a_command_we_gave_up_on_is_dropped(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The ledger keys belief on (slave, ticket, op), not on the command id. Without this check
    a stranded answer would be applied to whatever command now holds that key, and describe the
    wrong order."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    with caplog.at_level("ERROR"):
        answer(engine, cmd_q.sent[0], command_id=999)

    assert engine.believed_copies() == 0, "belief must not move on an answer we did not await"
    assert any("we are waiting on" in r.getMessage() for r in caplog.records)


def test_a_result_arriving_twice_is_only_believed_once(engine: Engine) -> None:
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    cmd = cmd_q.sent[0]

    answer(engine, cmd)
    answer(engine, cmd)

    assert engine.believed_copies() == 1


def test_a_command_that_cannot_be_queued_fails_immediately(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Leaving it in flight would delay the same outcome by the full timeout while nothing is
    coming, and look like a broker that went quiet."""
    go_live(engine)
    wire_slave(engine)
    arm_slave(engine, accept=False)
    watch(engine)

    with caplog.at_level("ERROR"):
        engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert engine._ledger.inflight_count == 0
    assert engine.failed_total == 1
    assert engine.believed_copies() == 0
    assert any("would not accept" in r.getMessage() for r in caplog.records)


def test_a_failed_open_is_not_retried(engine: Engine) -> None:
    """A decline is final. Retrying a permanent failure repeats it on every tick; retrying a
    transient one places the trade long after the price has moved."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    answer(engine, cmd_q.sent[0], ok=False)

    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert len(cmd_q.sent) == 1, "the ticket is excluded, not attempted again"
    assert engine.failed_total == 1


def test_a_held_slave_is_never_dispatched_to(engine: Engine) -> None:
    """The hold has to bite before dispatch, not merely before belief."""
    go_live(engine)
    wire_slave(engine, open_positions=(pos(ticket=999, magic=COPIER_MAGIC),))
    cmd_q = arm_slave(engine)
    watch(engine)

    engine._reconcile(snap(open_positions=(pos(ticket=11),)))

    assert cmd_q.sent == []
    assert set(engine.holds()) == {TID_SLAVE}


# --- giving up a slave's copies -------------------------------------------------------------
#
# An edit to a terminal that holds copies stops the copier managing them. They stay open on the
# slave; what changes is that nothing here closes them, moves their stops or mirrors a partial
# close -- and, the part that is easy to get wrong, that the master trades they were copied from
# are not copied a second time.
#
# A note on the tests below: the ledger drops an action identical to the one it last reported, so
# re-deriving the very same OPEN is silent whether or not the ticket is excluded. Each "not copied
# again" test therefore has the master change something first, so the re-derived action differs
# and only the exclusion stands between it and a duplicate.


def test_giving_up_a_slave_lets_go_of_its_positions_and_its_orders() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock, [pending_link()])
    ledger.step(
        [pos(ticket=5)],
        [pending_link()],
        master_orders=[order(ticket=50)],
        now=clock.advance(1.0),
    )
    assert [c.master_ticket for c in ledger.believed()] == [5]
    assert [c.master_ticket for c in ledger.believed_orders()] == [50]

    let_go = ledger.abandon(TID_SLAVE)

    assert [(c.master_ticket, c.resting) for c in let_go] == [(5, False), (50, True)]
    assert ledger.believed() == []
    assert ledger.believed_orders() == []


def test_giving_up_a_slave_with_nothing_open_lets_go_of_nothing() -> None:
    assert CopyLedger().abandon(TID_SLAVE) == []


def test_giving_up_touches_only_that_slave() -> None:
    clock = Clock()
    ledger = CopyLedger()
    links = [link(), link(terminal_id=TID_SLAVE2)]
    watching(ledger, clock, links)
    ledger.step([pos(ticket=5)], links, now=clock.advance(1.0))
    assert {c.terminal_id for c in ledger.believed()} == {TID_SLAVE, TID_SLAVE2}

    ledger.abandon(TID_SLAVE)

    assert [c.terminal_id for c in ledger.believed()] == [TID_SLAVE2]
    closed = ledger.step([], links, now=clock.advance(1.0))
    assert [(a.kind, a.terminal_id) for a in closed] == [(ActionKind.CLOSE, TID_SLAVE2)], (
        "the other slave goes on being managed"
    )


def test_a_master_trade_that_was_let_go_is_not_copied_a_second_time() -> None:
    """The master still holds it. With no row and no exclusion it reads as a trade that has just
    appeared, and the copy that was let go would be doubled."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5, sl=1.08000)], [link()], now=clock.advance(1.0))
    ledger.abandon(TID_SLAVE)

    again = ledger.step([pos(ticket=5, sl=1.07000)], [link()], now=clock.advance(1.0))

    assert again == []
    assert ledger.believed() == []


def test_a_copy_let_go_is_not_closed_or_moved_by_the_master() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5, sl=1.08000)], [link()], now=clock.advance(1.0))
    ledger.abandon(TID_SLAVE)

    assert ledger.step([pos(ticket=5, sl=1.09000)], [link()], now=clock.advance(1.0)) == []
    assert (
        ledger.step([pos(ticket=5, sl=1.09000, volume=0.5)], [link()], now=clock.advance(1.0)) == []
    )
    assert ledger.step([], [link()], now=clock.advance(1.0)) == [], "no CLOSE: nothing is tracked"


def test_a_resting_order_that_was_let_go_is_not_placed_a_second_time() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock, [pending_link()])
    ledger.step([], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0))
    ledger.abandon(TID_SLAVE)

    moved = order(ticket=50, price_open=1.07000)
    again = ledger.step([], [pending_link()], master_orders=[moved], now=clock.advance(1.0))

    assert again == []
    assert ledger.believed_orders() == []


def test_the_fill_of_a_resting_order_that_was_let_go_is_not_copied() -> None:
    """The slave's own order fills by itself, and MT5 gives the position it becomes the order's
    ticket. When the master's order fills, that position reads as brand new -- unless the ticket
    was excluded as a position too -- and copying it would double what the orphan already is."""
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock, [pending_link()])
    ledger.step([], [pending_link()], master_orders=[order(ticket=50)], now=clock.advance(1.0))
    ledger.abandon(TID_SLAVE)

    filled = ledger.step(
        [pos(ticket=50)], [pending_link()], master_orders=[], now=clock.advance(1.0)
    )

    assert filled == []
    assert ledger.believed() == []


def test_a_trade_the_master_opens_after_giving_up_is_copied_normally() -> None:
    clock = Clock()
    ledger = CopyLedger()
    watching(ledger, clock)
    ledger.step([pos(ticket=5)], [link()], now=clock.advance(1.0))
    ledger.abandon(TID_SLAVE)

    later = ledger.step([pos(ticket=5), pos(ticket=6)], [link()], now=clock.advance(1.0))

    assert [(a.kind, a.master_ticket) for a in later] == [(ActionKind.OPEN, 6)]


# --- what a settings edit does to a terminal that holds copies ------------------------------
#
# Every edit goes in as one atomic swap. An edit to a terminal that holds copies is only made by
# giving them up -- for every setting alike, so nobody has to know which are safe -- and only
# after the person has been shown exactly which copies, and agreed.

TID_OTHER = "C" * 32


def edited(eng: Engine, **changes: object) -> TerminalConfig:
    return eng.config.terminals[TID_SLAVE].model_copy(update=changes)


def order_row(**changes: object) -> Copy:
    return replace(
        stale_row(master_ticket=12, slave_ticket=901),
        kind=CopyKind.ORDER,
        order_type=OrderType.SELL_STOP,
        entry=1.07,
        **changes,  # type: ignore[arg-type]
    )


def held_by(eng: Engine, terminal_id: str = TID_SLAVE) -> list[int]:
    copies = eng.open_copies(terminal_id)
    assert copies is not None
    return [c.master_ticket for c in copies]


def test_copies_are_listed_from_the_store_before_detection_has_started(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """The store opens lazily, on the first reconcile, so until then the ledger is an empty
    in-memory one. Reading it would say nothing is held about a slave whose rows are alive, and
    the edit would go through without the person ever being asked."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    assert eng.believed_copies() == 0, "the ledger has not looked at the store yet"
    (copy,) = eng.open_copies(TID_SLAVE) or []
    assert (copy.master_ticket, copy.slave_ticket, copy.symbol, copy.volume, copy.resting) == (
        11,
        900,
        "EURUSD",
        0.5,
        False,
    )


def test_copies_are_listed_per_slave(engine_cfg: AppConfig, tmp_path: Path) -> None:
    seed_live_store(tmp_path, stale_row())
    seed_live_store(
        tmp_path, replace(stale_row(master_ticket=12, slave_ticket=901), terminal_id=TID_OTHER)
    )
    eng = live_engine(engine_cfg, tmp_path)
    assert held_by(eng, TID_SLAVE) == [11]
    assert held_by(eng, TID_OTHER) == [12]
    assert held_by(eng, "D" * 32) == []


def test_a_resting_order_is_a_copy_too(engine_cfg: AppConfig, tmp_path: Path) -> None:
    """A reversed resting order was placed the other way up as well: BUY_LIMIT becomes SELL_STOP."""
    seed_live_store(tmp_path, stale_row())
    seed_live_store(tmp_path, order_row())
    eng = live_engine(engine_cfg, tmp_path)
    copies = eng.open_copies(TID_SLAVE) or []
    assert [(c.master_ticket, c.resting) for c in copies] == [(11, False), (12, True)]


def test_an_unreadable_store_is_not_an_empty_one(
    engine_cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = live_engine(engine_cfg, tmp_path)
    monkeypatch.setattr(eng, "_ensure_store", lambda: (_ for _ in ()).throw(OSError("locked")))
    assert eng.open_copies(TID_SLAVE) is None


def test_an_edit_goes_in_whole(engine: Engine) -> None:
    new = edited(engine, risk_percent=0.5, copy_pending=True)
    assert engine.apply_settings(new) is None
    assert engine.config.terminals[TID_SLAVE] is new


def test_an_edit_that_changes_nothing_is_not_even_read_against_the_store(
    engine_cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = live_engine(engine_cfg, tmp_path)
    monkeypatch.setattr(eng, "_ensure_store", lambda: (_ for _ in ()).throw(OSError("locked")))
    assert eng.apply_settings(edited(eng)) is None


def test_an_edit_to_a_slave_with_no_copies_goes_in_without_being_asked_about(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    eng = live_engine(engine_cfg, tmp_path)
    assert eng.apply_settings(edited(eng, invert=True, risk_percent=0.25)) is None
    assert eng.config.terminals[TID_SLAVE].invert is True


@pytest.mark.parametrize(
    "change",
    [
        {"invert": True},
        {"risk_percent": 0.25},
        {"max_lot": 3.0},
        {"enabled": False},
        {"copy_sltp": False},
        {"copy_partials": False},
        {"copy_pending": True},
        {"symbol_suffix": "p"},
        {"symbol_blacklist": ["EURUSD"]},
        {"allow_live_trading": True},
    ],
    ids=lambda c: next(iter(c)),
)
def test_every_setting_is_refused_under_a_copy_until_it_is_agreed_to(
    engine_cfg: AppConfig, tmp_path: Path, change: dict[str, object]
) -> None:
    """One rule for all of them. Which are safe under an open copy is not something to know."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    before = eng.config.terminals[TID_SLAVE]

    refusal = eng.apply_settings(edited(eng, **change))

    assert refusal is not None
    assert "1 open copy" in refusal
    assert "confirmed" in refusal
    assert eng.config.terminals[TID_SLAVE] is before, "a refusal must leave the slave untouched"
    assert held_by(eng) == [11], "and must not have let anything go"


def test_a_resting_order_alone_is_enough_to_ask(engine_cfg: AppConfig, tmp_path: Path) -> None:
    seed_live_store(tmp_path, order_row())
    eng = live_engine(engine_cfg, tmp_path)
    assert eng.apply_settings(edited(eng, invert=True)) is not None


def test_agreeing_gives_the_copies_up_and_applies_the_edit(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    seed_live_store(tmp_path, stale_row())
    seed_live_store(tmp_path, order_row())
    eng = live_engine(engine_cfg, tmp_path)
    new = edited(eng, invert=True)

    assert eng.apply_settings(new, orphan={TID_SLAVE: [11, 12]}) is None

    assert eng.config.terminals[TID_SLAVE] is new
    assert held_by(eng) == [], "no longer tracked"


def test_what_was_given_up_is_reported_as_orphans_and_the_slave_is_not_held(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """Listed like an orphan found at startup -- open, unmanaged -- but already acknowledged, since
    the person agreed to it. So the slave goes on copying under its new settings."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    assert eng.apply_settings(edited(eng, risk_percent=0.5), orphan={TID_SLAVE: [11]}) is None

    (orphan,) = eng.orphans(TID_SLAVE)
    assert (orphan.slave_ticket, orphan.symbol, orphan.volume) == (900, "EURUSD", 0.5)
    assert eng.holds() == {}


def test_the_orphans_survive_the_next_reconciliation_still_acknowledged(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """The first tick runs startup reconciliation, which recomputes orphans from the slave's real
    positions. What was given up is one of them by definition, and it must not turn into a hold."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    cmd_q = arm_slave(eng)
    wire_slave(eng, open_positions=(pos(ticket=900, volume=0.5, magic=COPIER_MAGIC),))
    assert eng.apply_settings(edited(eng, risk_percent=0.5), orphan={TID_SLAVE: [11]}) is None

    eng._reconcile(snap(open_positions=(pos(ticket=11, sl=0.0),)))

    assert [o.slave_ticket for o in eng.orphans(TID_SLAVE)] == [900]
    assert eng.holds() == {}
    assert cmd_q.sent == []


def watched_copy_of_11(engine_cfg: AppConfig, tmp_path: Path) -> tuple[Engine, _FakeQueue]:
    """A live engine whose slave holds a copy of the master's trade 11, after one tick with the
    master holding it too. Continuity is established, and the tick derives nothing: the copy
    matches the master, which is what makes anything sent afterwards down to the edit."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    cmd_q = arm_slave(eng)
    wire_slave(eng, open_positions=(pos(ticket=900, volume=0.5, magic=COPIER_MAGIC),))
    eng._reconcile(snap(open_positions=(pos(ticket=11, sl=0.0),)))
    assert cmd_q.sent == []
    return eng, cmd_q


def test_the_master_trade_is_not_copied_again_after_the_engine_gives_it_up(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """The whole point of excluding the ticket. The master still holds trade 11, we watched it
    appear, and its row is gone: the next tick would open it again. The master gives it a stop so
    that the re-derived open is a different action from anything reported before."""
    eng, cmd_q = watched_copy_of_11(engine_cfg, tmp_path)
    assert eng.apply_settings(edited(eng, risk_percent=0.5), orphan={TID_SLAVE: [11]}) is None

    eng._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))

    assert cmd_q.sent == [], "nothing may be sent for a trade that was let go"


def test_a_trade_opened_after_giving_up_is_copied_under_the_new_settings(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    eng, cmd_q = watched_copy_of_11(engine_cfg, tmp_path)
    assert eng.apply_settings(edited(eng, risk_percent=0.5), orphan={TID_SLAVE: [11]}) is None

    eng._reconcile(snap(open_positions=(pos(ticket=11, sl=0.0), pos(ticket=12, sl=1.08))))

    assert [c.master_ticket for c in cmd_q.sent] == [12]  # type: ignore[attr-defined]


def test_agreeing_to_a_different_set_of_copies_is_refused(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """One opened while the person was deciding. They were not told about it, so giving it up is
    not what they agreed to."""
    seed_live_store(tmp_path, stale_row(master_ticket=11, slave_ticket=900))
    seed_live_store(tmp_path, stale_row(master_ticket=13, slave_ticket=903))
    eng = live_engine(engine_cfg, tmp_path)
    before = eng.config.terminals[TID_SLAVE]

    refusal = eng.apply_settings(edited(eng, invert=True), orphan={TID_SLAVE: [11]})

    assert refusal is not None
    assert "changed while you were deciding" in refusal
    assert eng.config.terminals[TID_SLAVE] is before
    assert held_by(eng) == [11, 13], "nothing was let go"


def test_agreeing_to_a_copy_that_is_gone_is_refused_too(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    refusal = eng.apply_settings(edited(eng, invert=True), orphan={TID_SLAVE: [11, 99]})
    assert refusal is not None
    assert held_by(eng) == [11]


def test_it_is_refused_while_a_command_is_still_in_flight(engine: Engine) -> None:
    """A result that arrives for a copy already let go would find no row to update."""
    go_live(engine)
    wire_slave(engine)
    cmd_q = arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    answer(engine, cmd_q.sent[0])
    engine._reconcile(snap(open_positions=(pos(ticket=11), pos(ticket=12))))
    assert len(cmd_q.sent) == 2, "the second open is out and unanswered"
    before = engine.config.terminals[TID_SLAVE]

    refusal = engine.apply_settings(edited(engine, invert=True), orphan={TID_SLAVE: [11]})

    assert refusal is not None
    assert "in flight" in refusal
    assert engine.config.terminals[TID_SLAVE] is before
    assert held_by(engine) == [11]


def test_an_open_in_flight_does_not_stop_an_edit_that_gives_nothing_up(engine: Engine) -> None:
    """Nothing is held yet, so there is nothing to let go of. The open lands under the new
    settings, which is what the person asked for."""
    go_live(engine)
    wire_slave(engine)
    arm_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    assert engine.believed_copies() == 0

    assert engine.apply_settings(edited(engine, invert=True)) is None


def test_it_is_refused_when_the_copies_cannot_be_read(
    engine_cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'We could not tell' is not a reason to change a slave that may be holding copies."""
    eng = live_engine(engine_cfg, tmp_path)
    monkeypatch.setattr(eng, "_ensure_store", lambda: (_ for _ in ()).throw(OSError("locked")))
    before = eng.config.terminals[TID_SLAVE]
    refusal = eng.apply_settings(edited(eng, invert=True))
    assert refusal is not None
    assert "could not be read" in refusal
    assert eng.config.terminals[TID_SLAVE] is before


def test_a_failure_part_way_is_a_refusal_and_not_a_crash(
    engine_cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    before = eng.config.terminals[TID_SLAVE]

    def boom(self: CopyLedger, terminal_id: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(CopyLedger, "abandon", boom)

    refusal = eng.apply_settings(edited(eng, invert=True), orphan={TID_SLAVE: [11]})

    assert refusal is not None
    assert "part-way" in refusal
    assert eng.config.terminals[TID_SLAVE] is before, "the settings were not applied"


def test_a_role_change_is_refused_before_anything_is_given_up(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """replace_terminal refuses it too, but only at the end -- after the copies had gone."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    with pytest.raises(ValueError, match="role"):
        eng.apply_settings(edited(eng, role=Role.UNASSIGNED), orphan={TID_SLAVE: [11]})
    assert held_by(eng) == [11]


def test_a_dry_run_lets_go_of_its_simulated_copies_and_reports_no_orphans(engine: Engine) -> None:
    """Nothing real is open in a dry run, so there is nothing to tell anyone about -- but the
    rehearsal must still do what live would, or it stops being one."""
    wire_slave(engine)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.08),)))
    assert engine.believed_copies() == 1
    seen = engine.action_total

    assert engine.apply_settings(edited(engine, risk_percent=0.5), orphan={TID_SLAVE: [11]}) is None

    assert engine.believed_copies() == 0
    assert engine.orphans() == []
    engine._reconcile(snap(open_positions=(pos(ticket=11, sl=1.07),)))
    assert engine.action_total == seen, "and it is not simulated a second time"


def test_the_check_and_the_swap_happen_under_the_drain_threads_lock(engine: Engine) -> None:
    """The drain thread dispatches under this lock. Counting first and swapping afterwards would
    leave a gap for an open to slip into, so the swap has to wait its turn."""
    started, done = threading.Event(), threading.Event()

    def edit() -> None:
        started.set()
        engine.apply_settings(edited(engine, risk_percent=0.5))
        done.set()

    worker = threading.Thread(target=edit)
    with engine._lock:
        worker.start()
        assert started.wait(2)
        assert not done.wait(0.3), "the edit must not proceed while the lock is held elsewhere"
        assert engine.config.terminals[TID_SLAVE].risk_percent == 1.0
    assert done.wait(2)
    worker.join()
    assert engine.config.terminals[TID_SLAVE].risk_percent == 0.5


# --- unassigning gives up open copies, the same way a settings edit does ----------------------


def stake_tickets(eng: Engine, terminal_id: str, new: TerminalConfig | None = None) -> object:
    stake = eng.copies_at_stake(terminal_id, new)
    assert stake is not None
    return {tid: [c.master_ticket for c in held] for tid, held in stake.items()}


def test_unassigning_a_slave_with_copies_is_refused_until_it_is_agreed_to(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)

    refusal = eng.unassign(TID_SLAVE)

    assert refusal is not None and "1 open copy" in refusal
    assert eng.config.terminals[TID_SLAVE].role is Role.SLAVE, "nothing changed"
    assert held_by(eng) == [11], "and nothing was let go"


def test_agreeing_unassigns_the_slave_and_lists_its_copies_as_orphans(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)

    assert eng.unassign(TID_SLAVE, orphan={TID_SLAVE: [11]}) is None

    assert eng.config.terminals[TID_SLAVE].role is Role.UNASSIGNED
    assert eng.config.terminals[TID_MASTER].role is Role.MASTER
    assert held_by(eng) == [], "no record is left behind to act on later"
    assert [o.slave_ticket for o in eng.orphans(TID_SLAVE)] == [900]


def test_unassigning_a_slave_with_nothing_open_asks_nothing(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    eng = live_engine(engine_cfg, tmp_path)
    assert eng.copies_at_stake(TID_SLAVE) == {}
    assert eng.unassign(TID_SLAVE) is None
    assert eng.config.terminals[TID_SLAVE].role is Role.UNASSIGNED


def test_unassigning_the_master_stakes_every_slaves_copies(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """The master holds none itself, but unassigning it unassigns its slaves, so theirs go."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)

    assert stake_tickets(eng, TID_MASTER) == {TID_SLAVE: [11]}
    refusal = eng.unassign(TID_MASTER)

    assert refusal is not None and "confirmed" in refusal
    assert eng.config.terminals[TID_MASTER].role is Role.MASTER
    assert eng.config.terminals[TID_SLAVE].role is Role.SLAVE


def test_switching_master_no_longer_leaves_records_behind_to_close(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """The bug this rule closes. Records are filed by master *ticket*, not by master, so under the
    next master -- which has none of those tickets -- the first tick proposed a CLOSE for every
    one. Unassigning the old master now gives them up, so there is nothing left to close."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)

    assert eng.unassign(TID_MASTER, orphan={TID_SLAVE: [11]}) is None

    assert eng.config.terminals[TID_MASTER].role is Role.UNASSIGNED
    assert eng.config.terminals[TID_SLAVE].role is Role.UNASSIGNED, "swept with its master"
    assert held_by(eng) == []
    assert [o.slave_ticket for o in eng.orphans(TID_SLAVE)] == [900]


def test_an_agreement_naming_the_wrong_slave_is_refused(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)

    refusal = eng.unassign(TID_MASTER, orphan={TID_OTHER: [11]})

    assert refusal is not None and "changed while you were deciding" in refusal
    assert held_by(eng) == [11]


def test_disabling_the_master_stakes_every_slaves_copies(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    """A disabled master is not watched, so nothing is mirrored onto its slaves' copies."""
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    off = eng.config.terminals[TID_MASTER].model_copy(update={"enabled": False})

    assert stake_tickets(eng, TID_MASTER, off) == {TID_SLAVE: [11]}
    assert eng.apply_settings(off) is not None
    assert eng.config.terminals[TID_MASTER].enabled is True

    assert eng.apply_settings(off, orphan={TID_SLAVE: [11]}) is None
    assert eng.config.terminals[TID_MASTER].enabled is False
    assert held_by(eng) == []


def test_any_other_edit_to_the_master_stakes_nothing(engine_cfg: AppConfig, tmp_path: Path) -> None:
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    edit = eng.config.terminals[TID_MASTER].model_copy(update={"symbol_suffix": "p"})

    assert eng.copies_at_stake(TID_MASTER, edit) == {}
    assert eng.apply_settings(edit) is None
    assert held_by(eng) == [11], "the slave's copies are still managed"


# --- copy-health statistics ---------------------------------------------------------------------


def dispatch_open(eng: Engine, ticket: int = 11) -> OrderCommand:
    go_live(eng)
    wire_slave(eng)
    cmd_q = arm_slave(eng)
    watch(eng)
    eng._reconcile(snap(open_positions=(pos(ticket=ticket, price_open=1.08500),)))
    cmd = cmd_q.sent[-1]
    assert isinstance(cmd, OrderCommand)
    return cmd


def test_an_answered_open_is_timed_counted_and_measured_for_slippage(engine: Engine) -> None:
    cmd = dispatch_open(engine)
    engine._on_result(
        OrderResult(
            command_id=cmd.command_id,
            terminal_id=TID_SLAVE,
            op=cmd.op,
            master_ticket=cmd.master_ticket,
            ok=True,
            slave_ticket=5001,
            filled_volume=0.5,
            price=1.08504,
            point=0.00001,
            attempts=2,
        )
    )
    stats = engine.terminal_stats(TID_SLAVE)
    assert stats.succeeded == 1
    assert stats.retried == 1
    assert len(stats.latencies_ms) == 1
    assert stats.latencies_ms[0] >= 0
    assert stats.slippage_points == pytest.approx((4.0,)), "a buy filled 4 points higher"


def test_a_late_answer_is_counted_as_dropped_not_as_an_outcome(engine: Engine) -> None:
    cmd = dispatch_open(engine)
    answer(engine, cmd, command_id=cmd.command_id + 99)
    stats = engine.terminal_stats(TID_SLAVE)
    assert stats.dropped_late == 1
    assert stats.answered == 0


def test_a_command_the_queue_refused_counts_as_a_transient_failure(engine: Engine) -> None:
    go_live(engine)
    wire_slave(engine)
    arm_slave(engine, accept=False)
    watch(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11),)))
    stats = engine.terminal_stats(TID_SLAVE)
    assert stats.failed_transient == 1
    assert stats.latencies_ms == ()


def test_skipped_decisions_are_counted_by_reason(engine: Engine) -> None:
    wire_slave(engine)
    engine._reconcile(snap(open_positions=(pos(ticket=11), pos(ticket=12))))
    stats = engine.terminal_stats(TID_SLAVE)
    assert stats.skipped == ((SkipReason.PREEXISTING.value, 2),)


def test_the_stats_are_a_copy_the_ui_cannot_disturb(engine: Engine) -> None:
    cmd = dispatch_open(engine)
    before = engine.terminal_stats(TID_SLAVE)
    answer(engine, cmd)
    assert before.answered == 0
    assert engine.terminal_stats(TID_SLAVE).answered == 1
