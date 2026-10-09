"""Breakeven: the trigger maths, the floor in the diff, where it is stored, and the engine tick
that sets it.

The floor is the part that has to be right. The diff compares the master's stop against the stop
last sent, so a breakeven that were a one-off move would be undone on the very next tick.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from mt5copier.breakeven import (
    breakeven_level,
    breakeven_stop,
    buffer_distance,
    check_breakeven_price,
)
from mt5copier.config import AppConfig, BreakevenMode, Role, TerminalConfig, load
from mt5copier.engine import Engine
from mt5copier.protocol import (
    COPIER_MAGIC,
    ActionKind,
    OrderCommand,
    OrderResult,
    Position,
    PositionLive,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.reconcile import (
    Action,
    CopiedPosition,
    CopyLedger,
    SkipReason,
    SlaveLink,
    protective_sl,
    reconcile_slave,
)
from mt5copier.settings_edit import apply_edits
from mt5copier.store import Copy, CopyKind, MappingStore

TID_MASTER = "A" * 32
TID_SLAVE = "B" * 32
BUY, SELL = PositionType.BUY, PositionType.SELL


def slave(**kw: object) -> TerminalConfig:
    base: dict[str, object] = {"terminal_id": TID_SLAVE, "label": "Slave", "role": Role.SLAVE}
    base.update(kw)
    return TerminalConfig(**base)  # type: ignore[arg-type]


EURUSD_VALUE = 100_000.0
"""What a 1.0 move in EURUSD is worth on one standard lot, in USD."""


def copy_of(
    ptype: PositionType = BUY,
    sl: float = 1.08000,
    point: float = 0.00001,
    digits: int = 5,
    risk_distance: float = 0.00500,
    price_value: float = EURUSD_VALUE,
    **kw: object,
) -> CopiedPosition:
    return CopiedPosition(
        terminal_id=TID_SLAVE,
        master_ticket=11,
        slave_symbol="EURUSD",
        type=ptype,
        master_volume=1.0,
        synced_sl=sl,
        slave_ticket=900,
        volume=0.5,
        point=point,
        digits=digits,
        risk_distance=risk_distance,
        price_value=price_value,
        **kw,  # type: ignore[arg-type]
    )


def master_pos(ticket: int = 11, ptype: PositionType = BUY, sl: float = 1.08000) -> Position:
    return Position(
        ticket=ticket,
        symbol="EURUSD",
        type=ptype,
        volume=1.0,
        price_open=1.08500,
        sl=sl,
        tp=0.0,
        time=1_700_000_000,
    )


COSTS = {"commission_per_lot_round_turn": 7.0, "slippage_buffer_points": 3}
"""$7 round turn is 7 points on EURUSD, plus 3 points of slippage: a 10-point buffer."""
R = {"breakeven_mode": BreakevenMode.R}


# --- where the stop goes ------------------------------------------------------------------


def test_the_buffer_is_commission_plus_slippage() -> None:
    assert buffer_distance(copy_of(), slave(**COSTS)) == pytest.approx(0.00010)
    assert buffer_distance(copy_of(), slave(slippage_buffer_points=3)) == pytest.approx(0.00003)
    assert buffer_distance(copy_of(), slave()) == 0.0


def test_commission_is_the_same_distance_whatever_the_lot_size() -> None:
    """Commission is per lot and so is the price value, so the lots cancel."""
    small = replace(copy_of(), volume=0.01)
    big = replace(copy_of(), volume=50.0)
    assert buffer_distance(small, slave(**COSTS)) == buffer_distance(big, slave(**COSTS))


def test_a_commission_that_cannot_be_priced_gives_no_buffer_rather_than_a_guess() -> None:
    assert buffer_distance(copy_of(price_value=0.0), slave(**COSTS)) is None
    assert buffer_distance(copy_of(price_value=0.0), slave(slippage_buffer_points=3)) == (
        pytest.approx(0.00003)
    ), "with no commission set, nothing needs pricing"


def test_btcusd_commission_turns_into_dollars_of_price() -> None:
    """BlackBull BTCUSD: 2 digits, a 1.0 move is $1 per lot. $14 round turn plus 500 points
    ($5) of slippage puts the stop $19 past entry."""
    copy = copy_of(point=0.01, digits=2, price_value=1.0, sl=84675.19, risk_distance=142.81)
    cfg = slave(commission_per_lot_round_turn=14.0, slippage_buffer_points=500)
    assert buffer_distance(copy, cfg) == pytest.approx(19.0)
    assert breakeven_stop(copy, 84818.0, 19.0) == pytest.approx(84837.0)


def test_the_stop_is_rounded_away_from_entry_so_the_buffer_is_never_shaved() -> None:
    assert breakeven_stop(copy_of(), 1.08500, 0.000071) == pytest.approx(1.08508)
    assert breakeven_stop(copy_of(SELL), 1.08500, 0.000071) == pytest.approx(1.08492)
    assert breakeven_stop(copy_of(), 1.08500, 0.00010) == pytest.approx(1.08510), "on the grid"


# --- when it goes --------------------------------------------------------------------------


def test_off_never_triggers() -> None:
    assert breakeven_level(copy_of(), 1.08500, 1.09500, slave(**COSTS)) is None


@pytest.mark.parametrize("fraction", [0.3, 1.0])
def test_an_r_trigger_is_a_fraction_of_the_opening_stop_distance(fraction: float) -> None:
    cfg = slave(**R, **COSTS, breakeven_trigger_r=fraction)
    at = 1.08500 + fraction * 0.00500
    assert breakeven_level(copy_of(), 1.08500, at - 0.00002, cfg) is None
    assert breakeven_level(copy_of(), 1.08500, at, cfg) == pytest.approx(1.08510)


def test_a_sell_measures_downward_and_puts_the_buffer_below_entry() -> None:
    cfg = slave(**R, **COSTS, breakeven_trigger_r=0.2)
    copy = copy_of(SELL, sl=1.09000)
    assert breakeven_level(copy, 1.08500, 1.08450, cfg) is None
    # Exactly on the trigger: 0.0009999... against 0.001 must not miss on a rounding error.
    assert breakeven_level(copy, 1.08500, 1.08400, cfg) == pytest.approx(1.08490)


def test_an_r_trigger_never_fires_on_a_copy_that_opened_with_no_stop() -> None:
    cfg = slave(**R)
    assert breakeven_level(copy_of(risk_distance=0.0), 1.08500, 1.20000, cfg) is None


def test_a_trigger_inside_the_buffer_waits_until_the_price_is_past_the_stop() -> None:
    """0.3R of a 2-pip stop is under a 10-point buffer; firing then would put the stop at or
    beyond the price, which the broker refuses."""
    cfg = slave(**R, **COSTS, breakeven_trigger_r=0.3)
    copy = copy_of(risk_distance=0.00020)
    assert breakeven_level(copy, 1.08500, 1.08510, cfg) is None
    assert breakeven_level(copy, 1.08500, 1.08512, cfg) == pytest.approx(1.08510)


@pytest.mark.parametrize(
    "change",
    [
        {"breakeven_exempt": True},
        {"breakeven_sl": 1.08510},
        {"point": 0.0},
        {"price_value": 0.0},
    ],
    ids=["exempt", "already-triggered", "point-unknown", "commission-unpriced"],
)
def test_nothing_triggers_on_a_copy_that_cannot_or_must_not(change: dict[str, object]) -> None:
    cfg = slave(**R, **COSTS)
    copy = replace(copy_of(), **change)  # type: ignore[arg-type]
    assert breakeven_level(copy, 1.08500, 1.09500, cfg) is None


def test_a_hand_set_price_is_the_trigger_even_with_breakeven_off() -> None:
    copy = copy_of(breakeven_at=1.08700)
    cfg = slave(**COSTS)
    assert breakeven_level(copy, 1.08500, 1.08690, cfg) is None
    assert breakeven_level(copy, 1.08500, 1.08700, cfg) == pytest.approx(1.08510)


def test_a_hand_set_price_replaces_the_slaves_own_trigger() -> None:
    cfg = slave(**R, **COSTS, breakeven_trigger_r=0.2)
    copy = copy_of(breakeven_at=1.08800)
    assert breakeven_level(copy, 1.08500, 1.08650, cfg) is None, "0.2R is not the trigger now"
    assert breakeven_level(copy, 1.08500, 1.08800, cfg) is not None


def test_a_hand_set_price_has_to_be_past_entry_plus_costs() -> None:
    cfg = slave(**COSTS)
    assert check_breakeven_price(copy_of(), 1.08500, 1.08700, cfg) is None
    assert "above 1.0851" in (check_breakeven_price(copy_of(), 1.08500, 1.08505, cfg) or "")
    sell = copy_of(SELL, sl=1.09)
    assert check_breakeven_price(sell, 1.08500, 1.08300, cfg) is None
    assert "below" in (check_breakeven_price(sell, 1.08500, 1.08600, cfg) or "")
    unpriced = copy_of(price_value=0.0)
    assert "commission" in (check_breakeven_price(unpriced, 1.08500, 1.08700, cfg) or "")


# --- the floor in the diff ------------------------------------------------------------------


def test_protective_sl_picks_the_tighter_stop_by_direction() -> None:
    assert protective_sl(1.08000, 1.08510, BUY) == 1.08510
    assert protective_sl(1.08700, 1.08510, BUY) == 1.08700
    assert protective_sl(1.09000, 1.08490, SELL) == 1.08490
    assert protective_sl(1.08300, 1.08490, SELL) == 1.08300
    assert protective_sl(0.0, 1.08510, BUY) == 1.08510
    assert protective_sl(1.08000, 0.0, BUY) == 1.08000


def _modify(actions: list[Action]) -> Action:
    (only,) = [a for a in actions if a.kind is ActionKind.MODIFY_SLTP]
    return only


def test_a_floor_moves_the_stop_while_the_masters_stays_put() -> None:
    copy = copy_of(breakeven_sl=1.08510)
    a = _modify(reconcile_slave([master_pos(sl=1.08000)], [copy], slave()))
    assert a.executable
    assert a.sl == pytest.approx(1.08510)
    assert not a.unprotected


def test_once_the_floor_is_synced_the_masters_old_stop_is_reported_not_sent() -> None:
    copy = copy_of(sl=1.08510, breakeven_sl=1.08510)
    a = _modify(reconcile_slave([master_pos(sl=1.08000)], [copy], slave()))
    assert not a.executable
    assert a.skip_reason is SkipReason.BREAKEVEN_HOLDS
    assert a.sl == pytest.approx(1.08000), "the notice names the master's level"
    assert "only tightens" in a.describe()


def test_a_master_trailing_past_the_floor_is_followed() -> None:
    copy = copy_of(sl=1.08510, breakeven_sl=1.08510)
    a = _modify(reconcile_slave([master_pos(sl=1.08800)], [copy], slave()))
    assert a.sl == pytest.approx(1.08800)


def test_a_master_loosening_or_clearing_its_stop_is_ignored_once_floored() -> None:
    copy = copy_of(sl=1.08510, breakeven_sl=1.08510)
    for master_sl in (1.07500, 0.0):
        a = _modify(reconcile_slave([master_pos(sl=master_sl)], [copy], slave()))
        assert not a.executable and a.skip_reason is SkipReason.BREAKEVEN_HOLDS
        assert not a.unprotected, "a cleared master stop leaves the copy protected"


def test_a_master_stop_matching_the_floor_says_nothing() -> None:
    copy = copy_of(sl=1.08510, breakeven_sl=1.08510)
    assert reconcile_slave([master_pos(sl=1.08510)], [copy], slave()) == []


def test_the_held_notice_is_reported_once_per_master_level() -> None:
    ledger = CopyLedger()
    link = [SlaveLink(slave())]
    ledger.step([], link, now=0.0)
    ledger.step([master_pos()], link, now=1.0, simulate=False)
    ledger.apply_result(_result(ActionKind.OPEN, price=1.08500))
    ledger.set_breakeven(TID_SLAVE, 11, 1.08510)

    moved = ledger.step([master_pos()], link, now=2.0, simulate=False)
    assert [a.sl for a in moved] == [pytest.approx(1.08510)]
    ledger.apply_result(replace(_result(ActionKind.MODIFY_SLTP), command_id=2))

    def notices(now: float, sl: float) -> list[Action]:
        return [
            a
            for a in ledger.step([master_pos(sl=sl)], link, now=now, simulate=False)
            if a.skip_reason is SkipReason.BREAKEVEN_HOLDS
        ]

    assert len(notices(3.0, 1.08000)) == 1, "confirms the floor right after it lands"
    assert notices(4.0, 1.08000) == []
    assert len(notices(5.0, 1.07500)) == 1, "a new looser level is news"


def test_a_floor_applies_on_a_reversed_copy_in_its_own_direction() -> None:
    """The master buys, the copy sells: the floor sits below the copy's entry and only a lower
    stop is tighter."""
    copy = copy_of(SELL, sl=0.0, breakeven_sl=1.08490)
    a = _modify(reconcile_slave([master_pos(sl=1.08000)], [copy], slave(invert=True)))
    assert a.sl == pytest.approx(1.08490)


def test_a_floor_moves_the_stop_even_when_stop_moves_are_not_copied() -> None:
    copy = copy_of(sl=1.08000, synced_tp=1.09000, breakeven_sl=1.08510)
    a = _modify(reconcile_slave([master_pos(sl=1.07000)], [copy], slave(copy_sltp=False)))
    assert a.executable
    assert (a.sl, a.tp) == (pytest.approx(1.08510), 1.09000), "the master's levels stay ignored"


def test_with_stop_moves_not_copied_a_synced_floor_still_reports_the_masters_change() -> None:
    copy = copy_of(sl=1.08510, breakeven_sl=1.08510)
    a = _modify(reconcile_slave([master_pos(sl=1.08700)], [copy], slave(copy_sltp=False)))
    assert a.skip_reason is SkipReason.SLTP_SYNC_DISABLED


# --- belief ---------------------------------------------------------------------------------


def _result(op: ActionKind, *, price: float | None = None) -> OrderResult:
    return OrderResult(
        command_id=1,
        terminal_id=TID_SLAVE,
        op=op,
        master_ticket=11,
        ok=True,
        slave_ticket=900,
        slave_symbol="EURUSD",
        filled_volume=0.5,
        price=price,
        point=0.00001,
        digits=5,
        price_value=EURUSD_VALUE,
    )


def test_a_filled_open_records_point_digits_one_r_and_price_value() -> None:
    ledger = CopyLedger()
    ledger.step([], [SlaveLink(slave())], now=0.0)
    ledger.step([master_pos()], [SlaveLink(slave())], now=1.0, simulate=False)
    ledger.apply_result(_result(ActionKind.OPEN, price=1.08520))
    (copy,) = ledger.believed(TID_SLAVE)
    assert (copy.point, copy.digits) == (0.00001, 5)
    assert copy.risk_distance == pytest.approx(0.00520)
    assert copy.price_value == EURUSD_VALUE


def test_exempting_drops_a_floor_and_a_hand_set_price() -> None:
    ledger = CopyLedger()
    ledger.step([], [SlaveLink(slave())], now=0.0)
    ledger.step([master_pos()], [SlaveLink(slave())], now=1.0, simulate=False)
    ledger.apply_result(_result(ActionKind.OPEN, price=1.08500))
    ledger.set_breakeven(TID_SLAVE, 11, 1.08510)
    ledger.set_breakeven_at(TID_SLAVE, 11, 1.08700)

    assert ledger.set_breakeven_exempt(TID_SLAVE, 11, True)
    (copy,) = ledger.believed(TID_SLAVE)
    assert (copy.breakeven_exempt, copy.breakeven_sl, copy.breakeven_at) == (True, 0.0, 0.0)

    assert ledger.set_breakeven_at(TID_SLAVE, 11, 1.08700)
    (copy,) = ledger.believed(TID_SLAVE)
    assert not copy.breakeven_exempt, "asking for a breakeven lifts the exemption"
    assert not ledger.set_breakeven_exempt(TID_SLAVE, 99, True)


def test_the_store_keeps_every_breakeven_field(tmp_path: Path) -> None:
    with MappingStore(tmp_path / "m.db") as store:
        store.remember_position(
            terminal_id=TID_SLAVE,
            master_ticket=11,
            slave_symbol="EURUSD",
            direction=BUY,
            master_volume=1.0,
            slave_ticket=900,
            point=0.00001,
            digits=5,
            risk_distance=0.005,
            price_value=EURUSD_VALUE,
        )
        store.update(TID_SLAVE, 11, breakeven_sl=1.0851, breakeven_at=1.087)
    with MappingStore(tmp_path / "m.db") as store:
        copy = store.get_position(TID_SLAVE, 11)
    assert copy is not None
    assert (copy.point, copy.digits, copy.risk_distance) == (0.00001, 5, 0.005)
    assert copy.price_value == EURUSD_VALUE
    assert (copy.breakeven_sl, copy.breakeven_at, copy.breakeven_exempt) == (1.0851, 1.087, False)


def test_an_older_mapping_file_gains_the_columns_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE copies (
            terminal_id TEXT NOT NULL, master_ticket INTEGER NOT NULL, kind TEXT NOT NULL,
            slave_ticket INTEGER, slave_symbol TEXT NOT NULL, direction INTEGER NOT NULL,
            order_type INTEGER, master_volume REAL NOT NULL, slave_volume REAL,
            entry REAL NOT NULL DEFAULT 0, synced_sl REAL NOT NULL DEFAULT 0,
            synced_tp REAL NOT NULL DEFAULT 0, opened_at REAL NOT NULL,
            updated_at REAL NOT NULL, PRIMARY KEY (terminal_id, master_ticket))"""
    )
    conn.execute(
        "INSERT INTO copies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TID_SLAVE, 11, "position", 900, "EURUSD", 0, None, 1.0, 0.5, 0, 1.08, 0, 1.0, 1.0),
    )
    conn.commit()
    conn.close()

    with MappingStore(path) as store:
        copy = store.get_position(TID_SLAVE, 11)
    assert copy is not None
    assert copy.synced_sl == 1.08
    assert (copy.point, copy.price_value, copy.breakeven_sl) == (0.0, 0.0, 0.0)


# --- settings -------------------------------------------------------------------------------


def test_an_r_trigger_has_to_be_above_zero() -> None:
    with pytest.raises(ValidationError):
        slave(**R, breakeven_trigger_r=0)
    outcome = apply_edits(slave(), {"breakeven_mode": "r", "breakeven_trigger_r": "-1"})
    assert not outcome.ok
    assert "breakeven_trigger_r" in outcome.errors


def test_a_config_saved_with_the_retired_pips_trigger_loads_with_breakeven_off(
    tmp_path: Path,
) -> None:
    """Breakeven by pips existed for an afternoon. A config saved then must load, not be
    treated as corrupt and reset, and must not guess what R the pips meant."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "terminals": {
                    TID_SLAVE: {
                        "terminal_id": TID_SLAVE,
                        "role": "slave",
                        "risk_percent": 0.5,
                        "breakeven_mode": "pips",
                        "breakeven_trigger_pips": 10,
                        "breakeven_buffer_pips": 1,
                    }
                }
            }
        )
    )
    cfg = load(path)
    t = cfg.terminals[TID_SLAVE]
    assert t.breakeven_mode is BreakevenMode.OFF
    assert t.risk_percent == 0.5, "the rest of the settings survive"


# --- the engine -----------------------------------------------------------------------------


class _Q:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def put_nowait(self, item: object) -> None:
        self.sent.append(item)

    def close(self) -> None:
        pass


class _Proc:
    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        pass


class _Worker:
    def __init__(self) -> None:
        self.terminal_id = TID_SLAVE
        self.process = _Proc()
        self.cmd_q = _Q()
        self.started_at = time.monotonic()


@pytest.fixture
def live(tmp_path: Path) -> Engine:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    cfg = AppConfig(
        copying_enabled=True,
        dry_run=False,
        terminals={
            TID_MASTER: TerminalConfig(
                terminal_id=TID_MASTER, label="Master", exe_path=exe, role=Role.MASTER
            ),
            TID_SLAVE: TerminalConfig(
                terminal_id=TID_SLAVE,
                label="Slave",
                exe_path=exe,
                role=Role.SLAVE,
                breakeven_mode=BreakevenMode.R,
                breakeven_trigger_r=0.2,  # 10 pips of the 50-pip stop
                commission_per_lot_round_turn=7.0,
                slippage_buffer_points=3,
            ),
        },
    )
    with MappingStore(tmp_path / "mapping.db") as store:
        store.record(
            Copy(
                terminal_id=TID_SLAVE,
                master_ticket=11,
                kind=CopyKind.POSITION,
                slave_symbol="EURUSD",
                direction=BUY,
                master_volume=1.0,
                slave_ticket=900,
                slave_volume=0.5,
                synced_sl=1.08000,
                point=0.00001,
                digits=5,
                risk_distance=0.005,
                price_value=EURUSD_VALUE,
            )
        )
    eng = Engine(cfg, state_path=tmp_path / "dryrun.db", mapping_path=tmp_path / "mapping.db")
    eng._workers[TID_SLAVE] = _Worker()  # type: ignore[assignment]
    return eng


def _slave_at(eng: Engine, price_current: float) -> None:
    eng._snapshots[TID_SLAVE] = Snapshot(
        terminal_id=TID_SLAVE,
        state=WorkerState.CONNECTED,
        balance=10_000.0,
        open_positions=(
            Position(
                ticket=900,
                symbol="EURUSD",
                type=BUY,
                volume=0.5,
                price_open=1.08500,
                sl=1.08000,
                time=1_700_000_000,
                magic=COPIER_MAGIC,
            ),
        ),
        live_positions=(PositionLive(ticket=900, price_current=price_current),),
        pending_orders=(),
    )
    eng._received_at[TID_SLAVE] = time.monotonic()


def _tick(eng: Engine) -> list[OrderCommand]:
    eng._reconcile(
        Snapshot(
            terminal_id=TID_MASTER,
            state=WorkerState.CONNECTED,
            open_positions=(master_pos(),),
            pending_orders=(),
        )
    )
    q = eng._workers[TID_SLAVE].cmd_q
    assert isinstance(q, _Q)
    sent = [c for c in q.sent if isinstance(c, OrderCommand)]
    q.sent.clear()
    return sent


def test_crossing_the_trigger_sends_the_stop_to_entry_plus_buffer(live: Engine) -> None:
    _slave_at(live, 1.08550)
    assert _tick(live) == []

    _slave_at(live, 1.08610)
    (cmd,) = _tick(live)
    assert cmd.op is ActionKind.MODIFY_SLTP
    assert cmd.sl == pytest.approx(1.08510)

    live._on_result(
        OrderResult(
            command_id=cmd.command_id,
            terminal_id=TID_SLAVE,
            op=cmd.op,
            master_ticket=11,
            ok=True,
            slave_ticket=900,
        )
    )
    _slave_at(live, 1.08520)
    assert _tick(live) == [], "the price falling back does not undo it"


def test_a_hand_set_price_is_checked_against_the_copys_fill(live: Engine) -> None:
    _slave_at(live, 1.08550)
    _tick(live)
    assert "above" in (live.set_breakeven_at(TID_SLAVE, 11, 1.08505) or "")
    assert live.set_breakeven_at(TID_SLAVE, 11, 1.08540) is None
    (cmd,) = _tick(live)
    assert cmd.sl == pytest.approx(1.08510)


def test_an_exempt_copy_never_moves(live: Engine) -> None:
    _slave_at(live, 1.08550)
    _tick(live)
    assert live.set_breakeven_exempt(TID_SLAVE, 11, True)
    _slave_at(live, 1.09500)
    assert _tick(live) == []


def test_editing_breakeven_settings_keeps_the_open_copies(live: Engine) -> None:
    _slave_at(live, 1.08550)
    _tick(live)
    cfg = live.config.terminals[TID_SLAVE]
    assert (
        live.copies_at_stake(TID_SLAVE, cfg.model_copy(update={"breakeven_trigger_r": 0.5})) == {}
    )
    assert live.apply_settings(cfg.model_copy(update={"breakeven_mode": BreakevenMode.OFF})) is None
    assert live.believed_copies() == 1


def _answer(eng: Engine, cmd: OrderCommand, *, ok: bool = True) -> None:
    eng._on_result(
        OrderResult(
            command_id=cmd.command_id,
            terminal_id=TID_SLAVE,
            op=cmd.op,
            master_ticket=11,
            ok=ok,
            slave_ticket=900,
            retcode=None if ok else 10016,
            error=None if ok else "INVALID_STOPS",
        )
    )


def test_a_refused_breakeven_is_tried_again_once_the_price_has_more_room(
    live: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The BlackBull BTCUSD case, 2026-10-03: no stops level reported, a floating spread, and
    a breakeven stop refused as INVALID_STOPS. A refusal must neither be final nor retried on
    every tick."""
    _slave_at(live, 1.08610)  # 10 pips of room above the 1.08510 stop
    (cmd,) = _tick(live)
    with caplog.at_level("WARNING"):
        _answer(live, cmd, ok=False)
        assert _tick(live) == [], "back on the master's stop, which the slave already carries"
    assert any("tried again once the price is" in r.getMessage() for r in caplog.records)
    assert live._ledger.get_position(TID_SLAVE, 11).breakeven_sl == 0.0  # type: ignore[union-attr]

    _slave_at(live, 1.08650)  # 14 pips: not yet 1.5x the refused 10
    assert _tick(live) == []

    _slave_at(live, 1.08670)  # 16 pips
    (again,) = _tick(live)
    assert again.op is ActionKind.MODIFY_SLTP
    assert again.sl == pytest.approx(1.08510), "the same level, now with room to spare"
    _answer(live, again)
    assert live._ledger.get_position(TID_SLAVE, 11).synced_sl == pytest.approx(1.08510)  # type: ignore[union-attr]


def test_a_breakeven_set_again_at_a_level_already_tried_is_still_sent(live: Engine) -> None:
    """Exempting and re-enabling re-arms the same level. That move must go out, not be
    mistaken for a repeat of the one already reported."""
    _slave_at(live, 1.08610)
    (cmd,) = _tick(live)
    _answer(live, cmd)
    live.set_breakeven_exempt(TID_SLAVE, 11, True)
    (back,) = _tick(live)
    assert back.sl == pytest.approx(1.08000), "exempt: back on the master's stop"
    _answer(live, back)

    live.set_breakeven_exempt(TID_SLAVE, 11, False)
    (rearmed,) = _tick(live)
    assert rearmed.sl == pytest.approx(1.08510)


def test_a_copy_whose_commission_cannot_be_priced_says_so_once(
    live: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    store = live._ensure_store()
    store.update(TID_SLAVE, 11, price_value=0.0)
    with caplog.at_level("WARNING"):
        _slave_at(live, 1.09000)
        assert _tick(live) == []
        _tick(live)
    warned = [r for r in caplog.records if "will not go to breakeven" in r.getMessage()]
    assert len(warned) == 1
