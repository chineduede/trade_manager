"""An account rule closing open copies: the worst one at a time, or every one at once.

The part that has to be right is the pacing. Closing the worst copy books a loss the slave's
snapshot has not seen yet, so a second close decided on the stale numbers would close one copy
too many: the next worst goes only once the last close shows in the snapshot and the rule still
says to act.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from test_breakeven import (
    BUY,
    EURUSD_VALUE,
    TID_MASTER,
    TID_SLAVE,
    _slave_at,
    _tick,
    _Worker,
    master_pos,
)
from test_breakeven import live as live  # noqa: F401  (fixture)
from test_guards import KEY, NOW, snap

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.engine import Engine
from mt5copier.guards import AccountGuards, GuardBook, GuardRule, GuardsDoc, Verdict
from mt5copier.guards.book import CloseAct
from mt5copier.protocol import (
    COPIER_MAGIC,
    ActionKind,
    OrderCommand,
    OrderResult,
    Position,
    PositionLive,
    Snapshot,
    WorkerState,
)
from mt5copier.shared_db import SharedDb
from mt5copier.store import Copy, CopyKind, MappingStore

# --- the verdict ---------------------------------------------------------------------------------


def _book(tmp: Path, **given: Any) -> GuardBook:
    rule = GuardRule(id="fl", type="floating_loss", params=given)
    doc = GuardsDoc(accounts={KEY: AccountGuards(initial_balance=100_000.0, rules=(rule,))})
    return GuardBook(doc, SharedDb(tmp / "state.db"))


def test_past_its_buffer_it_closes_the_worst_and_holds_new_trades(tmp_path: Path) -> None:
    """The default floating loss: 2% of 100k is 2,000, acting from 80% of it (1,600)."""
    v = _book(tmp_path).verdict(KEY, snap(positions=((1.0, -1_700.0),)), NOW)
    (act,) = v.close_acts
    assert (act.rule_id, act.title, act.mode) == ("fl", "Floating loss", "close_worst")
    assert act.why == "at 85% of its limit"
    assert v.held_back == ("Floating loss at 85% of its limit",)


def test_below_its_buffer_it_lets_go(tmp_path: Path) -> None:
    v = _book(tmp_path).verdict(KEY, snap(positions=((1.0, -1_500.0),)), NOW)
    assert v.close_acts == () and v.held_back == ()


def test_warn_and_block_do_not_close_and_flatten_says_so(tmp_path: Path) -> None:
    s = snap(positions=((1.0, -1_700.0),))
    warned = _book(tmp_path, on_breach="warn").verdict(KEY, s, NOW)
    assert warned.close_acts == () and warned.warnings
    blocked = _book(tmp_path, on_breach="block_new").verdict(KEY, s, NOW)
    assert blocked.close_acts == () and blocked.held_back
    (act,) = _book(tmp_path, on_breach="flatten").verdict(KEY, s, NOW).close_acts
    assert act.mode == "flatten"


# --- the engine ----------------------------------------------------------------------------------


def _act(eng: Engine, mode: str) -> None:
    eng._verdicts[TID_SLAVE] = Verdict(
        close_acts=(CloseAct("fl", "Floating loss", mode, "at 92% of its limit"),)
    )


def _said(eng: Engine) -> list[str]:
    return [g.text() for e in eng.day_book() for o in e.outcomes for g in o.guard]


def test_flatten_closes_the_copy_and_says_why(live: Engine) -> None:  # noqa: F811
    _slave_at(live, 1.08400)
    _act(live, "flatten")
    (cmd,) = _tick(live)
    assert cmd.op is ActionKind.CLOSE
    assert any("Floating loss closed it: at 92% of its limit" in s for s in _said(live))


def test_the_worst_copy_of_one_is_that_one(live: Engine) -> None:  # noqa: F811
    _slave_pnl(live, {900: -75.0})
    _act(live, "close_worst")
    (cmd,) = _tick(live)
    assert cmd.op is ActionKind.CLOSE
    assert any("the worst copy, -75.00 floating" in s and "92%" in s for s in _said(live))


@pytest.fixture
def two(tmp_path: Path) -> Engine:
    """A live engine already believing in two copies, #11 on slave ticket 900 and #12 on 901."""
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
                terminal_id=TID_SLAVE, label="Slave", exe_path=exe, role=Role.SLAVE
            ),
        },
    )
    with MappingStore(tmp_path / "mapping.db") as store:
        for master_ticket, slave_ticket in ((11, 900), (12, 901)):
            store.record(
                Copy(
                    terminal_id=TID_SLAVE,
                    master_ticket=master_ticket,
                    kind=CopyKind.POSITION,
                    slave_symbol="EURUSD",
                    direction=BUY,
                    master_volume=1.0,
                    slave_ticket=slave_ticket,
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


def _slave_pnl(eng: Engine, pnl: dict[int, float | None]) -> None:
    """The slave's snapshot: one open EURUSD buy per ticket, each with its floating P/L."""
    eng._snapshots[TID_SLAVE] = Snapshot(
        terminal_id=TID_SLAVE,
        state=WorkerState.CONNECTED,
        balance=10_000.0,
        open_positions=tuple(
            Position(
                ticket=ticket,
                symbol="EURUSD",
                type=BUY,
                volume=0.5,
                price_open=1.08500,
                sl=1.08000,
                time=1_700_000_000,
                magic=COPIER_MAGIC,
            )
            for ticket in pnl
        ),
        live_positions=tuple(
            PositionLive(ticket=ticket, price_current=1.08400, profit=profit, swap=0.0)
            for ticket, profit in pnl.items()
        ),
        pending_orders=(),
    )
    eng._received_at[TID_SLAVE] = time.monotonic()


def _tick2(eng: Engine) -> list[OrderCommand]:
    eng._reconcile(
        Snapshot(
            terminal_id=TID_MASTER,
            state=WorkerState.CONNECTED,
            open_positions=(master_pos(11), master_pos(12)),
            pending_orders=(),
        )
    )
    q = eng._workers[TID_SLAVE].cmd_q  # type: ignore[union-attr]
    sent = [c for c in q.sent if isinstance(c, OrderCommand)]  # type: ignore[attr-defined]
    q.sent.clear()  # type: ignore[attr-defined]
    return sent


def _ok(eng: Engine, cmd: OrderCommand, master_ticket: int, slave_ticket: int) -> None:
    eng._on_result(
        OrderResult(
            command_id=cmd.command_id,
            terminal_id=TID_SLAVE,
            op=cmd.op,
            master_ticket=master_ticket,
            ok=True,
            slave_ticket=slave_ticket,
        )
    )


def test_the_deepest_loss_goes_first_and_one_at_a_time(two: Engine) -> None:
    _slave_pnl(two, {900: -120.0, 901: 30.0})
    _act(two, "close_worst")
    (cmd,) = _tick2(two)
    assert cmd.op is ActionKind.CLOSE and cmd.master_ticket == 11
    assert any("the worst copy, -120.00 floating" in s for s in _said(two))
    _ok(two, cmd, master_ticket=11, slave_ticket=900)

    # The snapshot still shows #900 open, so the rule has not seen that loss booked yet:
    # nothing more is closed on the stale numbers.
    assert _tick2(two) == []

    # The close lands in the snapshot and the rule still says to act: the next worst goes.
    _slave_pnl(two, {901: 30.0})
    (second,) = _tick2(two)
    assert second.op is ActionKind.CLOSE and second.master_ticket == 12
    _ok(two, second, master_ticket=12, slave_ticket=901)


def test_once_the_rule_lets_go_nothing_more_is_closed(two: Engine) -> None:
    _slave_pnl(two, {900: -120.0, 901: 30.0})
    _act(two, "close_worst")
    (cmd,) = _tick2(two)
    _ok(two, cmd, master_ticket=11, slave_ticket=900)
    _slave_pnl(two, {901: 30.0})
    two._verdicts[TID_SLAVE] = Verdict()
    assert _tick2(two) == []


def test_without_a_known_pnl_nothing_is_closed_yet(two: Engine) -> None:
    _slave_pnl(two, {900: None, 901: None})
    _act(two, "close_worst")
    assert _tick2(two) == []
