"""Supervisor tests.

``_spawn_worker`` is stubbed in the reconciliation tests so nothing is actually forked; the
drain-loop tests use the real ``multiprocessing.Queue`` but feed it from this process, which
exercises the ordering and error-retention logic without a child.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fake_workers import FakeProcess, FakeWorker

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.engine import Engine, TerminalView
from mt5copier.protocol import ActionKind, OrderResult, PositionType, Snapshot, WorkerState

TID_A = "A" * 32
TID_B = "B" * 32
TID_NOEXE = "C" * 32


@pytest.fixture
def exe(tmp_path: Path) -> Path:
    path = tmp_path / "terminal64.exe"
    path.write_bytes(b"MZ")
    return path


@pytest.fixture
def cfg(exe: Path) -> AppConfig:
    return AppConfig(
        terminals={
            TID_A: TerminalConfig(terminal_id=TID_A, label="Alpha", exe_path=exe, role=Role.MASTER),
            TID_B: TerminalConfig(terminal_id=TID_B, label="Beta", exe_path=exe, role=Role.SLAVE),
            TID_NOEXE: TerminalConfig(
                terminal_id=TID_NOEXE, label="Gone", exe_path=None, role=Role.SLAVE
            ),
        }
    )


@pytest.fixture
def engine(cfg: AppConfig, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Engine whose spawning is recorded rather than performed."""
    eng = Engine(cfg)
    spawned: list[str] = []

    def fake_spawn(tcfg: TerminalConfig) -> None:
        spawned.append(tcfg.terminal_id)
        eng._workers[tcfg.terminal_id] = FakeWorker(tcfg.terminal_id)  # type: ignore[assignment]
        eng._received_at[tcfg.terminal_id] = time.monotonic()

    monkeypatch.setattr(eng, "_spawn_worker", fake_spawn)
    eng.spawned = spawned  # type: ignore[attr-defined]
    return eng


# --- TerminalView semantics ----------------------------------------------------------------


def test_unsupervised_terminal_is_never_stale() -> None:
    """Staleness only means something for a worker we are actually running."""
    view = TerminalView(cfg=TerminalConfig(terminal_id=TID_A), supervised=False, received_at=None)
    assert view.is_stale(1.0) is False
    assert view.status(1.0) == "-"


def test_assigned_but_unsupervised_reads_as_not_connected() -> None:
    view = TerminalView(cfg=TerminalConfig(terminal_id=TID_A, role=Role.SLAVE), supervised=False)
    assert view.status(1.0) == "not connected"


def test_stale_when_snapshots_stop_arriving() -> None:
    view = TerminalView(
        cfg=TerminalConfig(terminal_id=TID_A, role=Role.SLAVE),
        supervised=True,
        received_at=time.monotonic() - 60,
        process_alive=True,
        snapshot=Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED),
    )
    assert view.is_stale(15.0) is True
    assert view.status(15.0) == "stale"


def test_disconnected_is_distinct_from_stale() -> None:
    """A terminal reporting DISCONNECTED is healthy plumbing with a dead broker link; a stale
    one is our own worker gone quiet. They need different fixes, so they must look different."""
    view = TerminalView(
        cfg=TerminalConfig(terminal_id=TID_A, role=Role.SLAVE),
        supervised=True,
        received_at=time.monotonic(),
        process_alive=True,
        snapshot=Snapshot(terminal_id=TID_A, state=WorkerState.DISCONNECTED),
    )
    assert view.is_stale(15.0) is False
    assert view.status(15.0) == "disconnected"


def test_dead_worker_process_is_reported() -> None:
    view = TerminalView(
        cfg=TerminalConfig(terminal_id=TID_A, role=Role.SLAVE),
        supervised=True,
        received_at=time.monotonic(),
        process_alive=False,
    )
    assert view.status(15.0) == "worker dead"


# --- reconciliation ------------------------------------------------------------------------


def test_sync_spawns_only_assigned_terminals(engine: Engine) -> None:
    engine.sync()
    assert set(engine.spawned) == {TID_A, TID_B}  # type: ignore[attr-defined]


def test_sync_skips_terminal_without_executable(engine: Engine) -> None:
    """An uninstalled terminal must never be connected to, even if it carries a role."""
    engine.sync()
    assert TID_NOEXE not in engine.spawned  # type: ignore[attr-defined]


def test_sync_is_idempotent(engine: Engine) -> None:
    engine.sync()
    engine.sync()
    assert engine.spawned.count(TID_A) == 1  # type: ignore[attr-defined]


def test_sync_stops_worker_when_role_removed(engine: Engine, cfg: AppConfig) -> None:
    engine.sync()
    cfg.set_role(TID_B, Role.UNASSIGNED)
    engine.sync()
    assert TID_B not in engine._workers


def test_sync_stops_worker_when_disabled(engine: Engine, cfg: AppConfig) -> None:
    engine.sync()
    cfg.terminals[TID_B].enabled = False
    engine.sync()
    assert TID_B not in engine._workers


def test_sync_respawns_a_dead_worker(engine: Engine) -> None:
    engine.sync()
    engine._workers[TID_A].process.alive = False  # type: ignore[attr-defined]
    engine.sync()
    assert engine.spawned.count(TID_A) == 2  # type: ignore[attr-defined]


def test_stop_closes_terminals_it_launched(engine: Engine) -> None:
    """Quitting the app is symmetric with starting it: anything we opened, we close."""
    engine.sync()
    worker = engine._workers[TID_A]
    engine.stop()
    assert worker.cmd_q.sent[-1].close_terminal is True  # type: ignore[attr-defined]


def test_sync_closes_the_terminal_when_a_role_is_removed(engine: Engine, cfg: AppConfig) -> None:
    """Unassigned for good, not a transient restart -- so done with it if we launched it."""
    engine.sync()
    worker = engine._workers[TID_B]
    cfg.set_role(TID_B, Role.UNASSIGNED)
    engine.sync()
    assert worker.cmd_q.sent[-1].close_terminal is True  # type: ignore[attr-defined]


def test_restart_never_asks_to_close_the_terminal(engine: Engine) -> None:
    """A restart wants the same terminal straight back, not a bounced window."""
    engine.sync()
    worker = engine._workers[TID_A]
    engine.restart(TID_A)
    assert worker.cmd_q.sent[-1].close_terminal is False  # type: ignore[attr-defined]


def test_stop_worker_escalates_to_terminate(engine: Engine) -> None:
    """A worker that ignores the stop command must still be cleaned up."""
    engine.sync()
    worker = engine._workers[TID_A]

    class Stubborn(FakeProcess):
        def join(self, timeout: float | None = None) -> None:
            pass  # never exits on its own

    worker.process = Stubborn()  # type: ignore[assignment]
    engine._stop_worker(TID_A)
    assert worker.process.terminated is True  # type: ignore[attr-defined]
    assert TID_A not in engine._workers


def test_health_counts(engine: Engine) -> None:
    engine.sync()
    health = engine.health()
    assert health["total"] == 3
    assert health["supervised"] == 2
    assert health["live"] == 0


def test_views_are_sorted_by_label(engine: Engine) -> None:
    assert [v.cfg.label for v in engine.views()] == ["Alpha", "Beta", "Gone"]


def test_views_return_copies(engine: Engine, cfg: AppConfig) -> None:
    """The UI holds views across the event loop, so mutating one must not touch real config."""
    view = engine.views()[0]
    view.cfg.risk_percent = 99.0
    assert cfg.terminals[TID_A].risk_percent == 1.0


# --- drain loop ----------------------------------------------------------------------------


def run_drain(engine: Engine, snapshots: list[object], settle: float = 1.5) -> None:
    """Feed items through the real queue and drain thread, then stop it.

    Takes ``object`` rather than ``Snapshot`` because one queue carries both snapshots and
    order results, and the drain loop has to tell them apart.
    """
    engine._stop_drain.clear()
    thread = threading.Thread(target=engine._drain_loop, daemon=True)
    thread.start()
    for snap in snapshots:
        engine._out_q.put(snap)

    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        with engine._lock:
            if len(engine._snapshots) >= 1 and engine._out_q.empty():
                break
        time.sleep(0.02)
    time.sleep(0.1)
    engine._stop_drain.set()
    thread.join(timeout=2.0)


def test_drain_stores_latest_snapshot(engine: Engine) -> None:
    run_drain(
        engine,
        [
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTING, seq=1),
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=2, equity=100.0),
        ],
    )
    assert engine._snapshots[TID_A].seq == 2
    assert engine._snapshots[TID_A].equity == 100.0


def test_drain_ignores_out_of_order_snapshots(engine: Engine) -> None:
    """Out-of-order delivery would otherwise let a stale CONNECTING overwrite a newer
    CONNECTED and make the UI flicker between states."""
    run_drain(
        engine,
        [
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=5, equity=100.0),
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTING, seq=2),
        ],
    )
    assert engine._snapshots[TID_A].seq == 5
    assert engine._snapshots[TID_A].state is WorkerState.CONNECTED


def test_a_stopped_workers_leftovers_cannot_lock_out_its_replacement(engine: Engine) -> None:
    """Turning a terminal off and on left it "stale" for good.

    The old worker's last snapshot drained after it was reaped, with a seq in the hundreds; the
    new worker counts from 0, so every snapshot it sent was refused as out of order.
    """
    engine.sync()
    engine._workers[TID_A].process.pid = 200  # type: ignore[attr-defined]
    run_drain(
        engine,
        [
            # Still queued from the worker that was replaced (pid 100).
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=900, worker_pid=100),
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=1, worker_pid=200),
        ],
    )
    assert engine._snapshots[TID_A].worker_pid == 200
    assert engine._snapshots[TID_A].seq == 1


def test_a_snapshot_for_a_terminal_with_no_worker_is_dropped(engine: Engine) -> None:
    """Drained after its worker was stopped: filing it would show the terminal as alive."""
    run_drain(
        engine,
        [Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=900, worker_pid=100)],
        settle=0.3,
    )
    assert TID_A not in engine._snapshots


def test_drain_records_an_account_switch_made_in_the_terminal_itself(
    engine: Engine, cfg: AppConfig
) -> None:
    """Logging into another account from MT5's own UI, under a running worker, shows up only
    in the snapshots. The config must follow, or the account's given name, the rename and the
    guards would all stay filed under the account the terminal left -- and it is saved at
    once, so a crash cannot resurrect the stale account."""
    cfg.account_names["222@Axi-US51-Live"] = "Axi second"
    run_drain(
        engine,
        [
            Snapshot(
                terminal_id=TID_B,
                state=WorkerState.CONNECTED,
                seq=1,
                login=111,
                server="Axi-US52-Live",
            ),
            Snapshot(
                terminal_id=TID_B,
                state=WorkerState.CONNECTED,
                seq=2,
                login=222,
                server="Axi-US51-Live",
            ),
        ],
    )
    tcfg = cfg.terminals[TID_B]
    assert (tcfg.last_login, tcfg.last_server) == (222, "Axi-US51-Live")
    assert cfg.display_name(TID_B) == "Axi second", "the new account's name shows at once"

    from mt5copier import config as config_mod

    saved = config_mod.load()
    assert saved.terminals[TID_B].last_login == 222, "recorded on disk, not only in memory"
    assert saved.terminals[TID_B].role is Role.UNASSIGNED, "the unassign is on disk too"


def test_a_slave_switched_in_the_terminal_is_unassigned_with_its_copies_given_up(
    engine: Engine, cfg: AppConfig
) -> None:
    """The app's own rule is that only an unassigned terminal is switched. A switch made in
    MT5 itself cannot be refused after the fact, so the state is made what the rule required:
    copies given up (open on the old account, excluded from re-copying), role removed, live
    override withdrawn -- it was confirmed for the account the terminal left."""
    cfg.replace_terminal(
        cfg.terminals[TID_B].model_copy(
            update={"last_login": 111, "last_server": "Old-Live", "allow_live_trading": True}
        )
    )
    engine._ledger._store.remember_position(
        terminal_id=TID_B,
        master_ticket=11,
        slave_symbol="EURUSD",
        direction=PositionType.BUY,
        master_volume=1.0,
        slave_ticket=900,
    )
    run_drain(
        engine,
        [
            Snapshot(
                terminal_id=TID_B, state=WorkerState.CONNECTED, seq=1, login=111, server="Old-Live"
            ),
            Snapshot(
                terminal_id=TID_B, state=WorkerState.CONNECTED, seq=2, login=222, server="New"
            ),
        ],
    )
    tcfg = cfg.terminals[TID_B]
    assert tcfg.role is Role.UNASSIGNED
    assert tcfg.allow_live_trading is False
    assert (tcfg.last_login, tcfg.last_server) == (222, "New")
    assert engine._ledger.open_copies(TID_B) == [], "the old account's copies are let go"
    assert 11 in engine._ledger._excluded.get(TID_B, set()), "and never re-copied"


def test_a_switched_master_stops_copying_before_the_tick_can_close_anything(
    engine: Engine, cfg: AppConfig
) -> None:
    """A switched master's old positions leaving the snapshot would read as 'the master went
    flat' and close every copy; the new account's positions would read as fresh trades. The
    switch lands before that same snapshot reaches _reconcile, which checks copying_enabled
    first, so the tick never runs. The role is kept: what the new account means is the
    person's call."""
    cfg.replace_terminal(
        cfg.terminals[TID_A].model_copy(update={"last_login": 111, "last_server": "Old-Live"})
    )
    cfg.copying_enabled = True
    run_drain(
        engine,
        [
            Snapshot(
                terminal_id=TID_A, state=WorkerState.CONNECTED, seq=1, login=111, server="Old-Live"
            ),
            Snapshot(
                terminal_id=TID_A, state=WorkerState.CONNECTED, seq=2, login=222, server="New"
            ),
        ],
    )
    assert cfg.copying_enabled is False
    assert cfg.terminals[TID_A].role is Role.MASTER
    assert cfg.terminals[TID_B].role is Role.SLAVE, "no slave is swept; nothing is closed"


def test_a_config_regression_is_re_recorded_without_reading_as_a_switch(
    engine: Engine, cfg: AppConfig
) -> None:
    """A rescan can write a stale common.ini login over a connected slave's config. The
    worker still reports the same account it always did, so that is bookkeeping to correct
    quietly -- unassigning the slave over it would be reacting to our own stale read."""
    run_drain(
        engine,
        [Snapshot(terminal_id=TID_B, state=WorkerState.CONNECTED, seq=1, login=111, server="S")],
    )
    cfg.replace_terminal(
        cfg.terminals[TID_B].model_copy(update={"last_login": 999, "last_server": "Stale"})
    )
    run_drain(
        engine,
        [Snapshot(terminal_id=TID_B, state=WorkerState.CONNECTED, seq=2, login=111, server="S")],
    )
    tcfg = cfg.terminals[TID_B]
    assert (tcfg.last_login, tcfg.last_server) == (111, "S"), "corrected back off the worker"
    assert tcfg.role is Role.SLAVE, "and never treated as an account switch"


def test_the_periodic_rescan_defers_to_a_live_workers_account(
    engine: Engine, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep merges what discovery read off disk, but a connected terminal's worker is
    the fresher truth: a stale common.ini must not regress its account between snapshots."""
    from mt5copier.discovery import DiscoveredTerminal

    run_drain(
        engine,
        [Snapshot(terminal_id=TID_B, state=WorkerState.CONNECTED, seq=1, login=111, server="S")],
    )
    saves: list[AppConfig] = []
    monkeypatch.setattr("mt5copier.engine.save_config", saves.append)
    monkeypatch.setattr(
        "mt5copier.engine.discover_terminals",
        lambda: [
            # TID_B's ini is stale (the account it left); TID_A's is news worth keeping.
            DiscoveredTerminal(
                terminal_id=TID_B,
                data_path=None,
                install_path=None,
                exe_path=None,
                label="Beta",
                login=999,
                server="Stale",
            ),
            DiscoveredTerminal(
                terminal_id=TID_A,
                data_path=None,
                install_path=None,
                exe_path=None,
                label="Alpha",
                login=777,
                server="Fresh",
            ),
        ],
    )
    engine._rescan_once()
    assert cfg.terminals[TID_B].last_login == 111, "the live worker's account stands"
    assert cfg.terminals[TID_A].last_login == 777, "no worker, so the disk sweep lands"
    assert len(saves) == 1
    engine._rescan_once()
    assert len(saves) == 1, "an unchanged sweep does not rewrite the config"


def test_recording_the_account_never_churns_while_it_is_unchanged(
    engine: Engine, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One save when the account is first learned; none for every later snapshot, and a
    snapshot whose worker did not read the server keeps the server on record. First sight of
    an account is learning, not a switch: the slave keeps its role and its override."""
    saves: list[AppConfig] = []
    monkeypatch.setattr("mt5copier.engine.save_config", saves.append)
    run_drain(
        engine,
        [
            Snapshot(terminal_id=TID_B, state=WorkerState.CONNECTED, seq=1, login=111, server="S"),
            Snapshot(terminal_id=TID_B, state=WorkerState.CONNECTED, seq=2, login=111, server="S"),
            Snapshot(terminal_id=TID_B, state=WorkerState.CONNECTED, seq=3, login=111),
        ],
    )
    tcfg = cfg.terminals[TID_B]
    assert (tcfg.last_login, tcfg.last_server) == (111, "S")
    assert tcfg.role is Role.SLAVE, "learning the first account is not a switch"
    assert len(saves) == 1


def test_drain_keeps_last_error_after_recovery(engine: Engine) -> None:
    """A transient failure should leave a visible trace rather than vanishing on reconnect."""
    run_drain(
        engine,
        [
            Snapshot(terminal_id=TID_A, state=WorkerState.FAILED, seq=1, error="IPC timeout"),
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=2),
        ],
    )
    snap = engine._snapshots[TID_A]
    assert snap.state is WorkerState.CONNECTED
    assert snap.error == "IPC timeout"


def test_drain_ignores_objects_it_does_not_recognise(engine: Engine) -> None:
    """Anything that is neither a snapshot nor an order result is dropped, not crashed on --
    the drain thread is what every terminal's liveness display depends on."""
    run_drain(
        engine,
        [
            "not a snapshot",
            42,
            None,
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=1),
        ],
    )
    assert set(engine._snapshots) == {TID_A}


def test_drain_consumes_order_results(engine: Engine) -> None:
    """Results share the queue with snapshots. Dropping them would leave every dispatched
    command in flight forever."""
    run_drain(
        engine,
        [
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=1),
            OrderResult(
                command_id=1,
                terminal_id=TID_A,
                op=ActionKind.OPEN,
                master_ticket=11,
                ok=False,
                error="refused",
            ),
        ],
    )
    assert [r.result.master_ticket for r in engine.recent_results()] == [11]
    assert engine.failed_total == 1


def test_drain_tracks_connection_uptime_and_disconnects(engine: Engine) -> None:
    run_drain(
        engine,
        [
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=1),
            Snapshot(terminal_id=TID_A, state=WorkerState.DISCONNECTED, seq=2),
            Snapshot(terminal_id=TID_A, state=WorkerState.CONNECTED, seq=3),
        ],
    )
    stats = engine.terminal_stats(TID_A)
    assert stats.disconnects == 1
    assert stats.connected_for_s is not None


def test_pausing_a_slave_keeps_it_connected_and_closes_nothing(
    engine: Engine, cfg: AppConfig
) -> None:
    engine.sync()
    engine.set_paused(TID_B, True)
    assert cfg.terminals[TID_B].paused is True
    engine.sync()
    assert TID_B in engine._workers, "a paused slave stays connected"
    assert not engine._workers[TID_B].cmd_q.sent, "and nothing was asked to stop"  # type: ignore[attr-defined]


def test_only_a_slave_can_be_paused_by_the_engine(engine: Engine) -> None:
    with pytest.raises(ValueError, match="only a slave"):
        engine.set_paused(TID_A, True)


@pytest.mark.parametrize(
    ("closing", "close", "close_any"),
    [("never", False, False), ("launched", True, False), ("all", True, True)],
)
def test_the_closing_setting_decides_what_quitting_and_unassigning_close(
    engine: Engine, cfg: AppConfig, closing: str, close: bool, close_any: bool
) -> None:
    from mt5copier.config import TerminalClosing

    cfg.close_terminals = TerminalClosing(closing)
    engine.sync()
    unassigned = engine._workers[TID_B]
    cfg.set_role(TID_B, Role.UNASSIGNED)
    engine.sync()
    quitting = engine._workers[TID_A]
    engine.stop()
    for worker in (unassigned, quitting):
        sent = worker.cmd_q.sent[-1]  # type: ignore[attr-defined]
        assert (sent.close_terminal, sent.close_any) == (close, close_any)


def test_a_restart_closes_nothing_whatever_the_setting(engine: Engine, cfg: AppConfig) -> None:
    from mt5copier.config import TerminalClosing

    cfg.close_terminals = TerminalClosing.ALL
    engine.sync()
    worker = engine._workers[TID_A]
    engine.restart(TID_A)
    sent = worker.cmd_q.sent[-1]  # type: ignore[attr-defined]
    assert (sent.close_terminal, sent.close_any) == (False, False)


# --- guards -------------------------------------------------------------------------------------


def test_guard_report_follows_the_account_logged_in(cfg: AppConfig, tmp_path: Path) -> None:
    from mt5copier.guards import AccountGuards, GuardRule, GuardsDoc, Level

    doc = GuardsDoc(
        accounts={
            "7@Prop": AccountGuards(
                initial_balance=10_000.0,
                rules=(GuardRule(type="max_drawdown", params={"limit": 10}),),
            )
        }
    )
    eng = Engine(cfg, guards=doc, guard_state_path=tmp_path / "state.json")
    assert eng.guard_report(TID_B).rules == (), "no account known yet"

    cfg.terminals[TID_B].last_login, cfg.terminals[TID_B].last_server = 7, "Prop"
    assert eng.guard_report(TID_B).rules[0].level is Level.UNKNOWN, "set up, not connected"

    eng._snapshots[TID_B] = Snapshot(
        terminal_id=TID_B,
        state=WorkerState.CONNECTED,
        login=7,
        server="Prop",
        balance=9_500.0,
        equity=9_400.0,
    )
    status = eng.guard_report(TID_B).rules[0]
    assert status.used == pytest.approx(600.0) and status.level is Level.OK


def test_a_guard_holds_a_slave_back_and_caps_its_risk(cfg: AppConfig, tmp_path: Path) -> None:
    from mt5copier.guards import AccountGuards, GuardRule, GuardsDoc
    from mt5copier.protocol import PositionType
    from mt5copier.reconcile import Action

    doc = GuardsDoc(
        accounts={
            "7@Prop": AccountGuards(
                initial_balance=10_000.0,
                rules=(
                    GuardRule(type="max_drawdown", params={"limit": 500, "unit": "money"}),
                    GuardRule(
                        type="risk_per_trade",
                        params={"limit": 40, "unit": "money", "on_breach": "shrink"},
                    ),
                ),
            )
        }
    )
    eng = Engine(cfg, guards=doc, guard_state_path=tmp_path / "state.json")
    snap = Snapshot(
        terminal_id=TID_B,
        state=WorkerState.CONNECTED,
        login=7,
        server="Prop",
        balance=10_000.0,
        equity=9_700.0,
        open_positions=(),
        pending_orders=(),
    )
    eng._judge(snap)
    assert eng._verdicts[TID_B].hold is None
    assert eng._gate(TID_B, snap) is not None

    eng._judge(replace(snap, equity=9_550.0))
    gate = eng._gate(TID_B, snap)
    assert gate is not None and gate.hold

    action = Action(
        kind=ActionKind.OPEN,
        terminal_id=TID_B,
        master_ticket=1,
        master_symbol="EURUSD",
        slave_symbol="EURUSD",
        type=PositionType.BUY,
        master_volume=1.0,
        entry=1.1,
        sl=1.09,
    )
    verdict = eng._verdicts[TID_B]
    risk, note = eng._capped_risk(action, cfg.terminals[TID_B], 100.0, verdict)
    assert risk == 40.0
    assert note is not None and note.action == "shrunk" and note.rule == "Risk per trade"


def test_a_stop_rule_set_to_warn_says_so_on_a_bare_trade(cfg: AppConfig, tmp_path: Path) -> None:
    from mt5copier.guards import AccountGuards, GuardRule, GuardsDoc
    from mt5copier.protocol import PositionType
    from mt5copier.reconcile import Action

    rule = GuardRule(id="st", type="stop_required", params={"on_breach": "warn"})
    doc = GuardsDoc(accounts={"7@Prop": AccountGuards(initial_balance=10_000.0, rules=(rule,))})
    eng = Engine(cfg, guards=doc, guard_state_path=tmp_path / "state.json")
    snap = Snapshot(
        terminal_id=TID_B,
        state=WorkerState.CONNECTED,
        login=7,
        server="Prop",
        balance=10_000.0,
        equity=10_000.0,
        open_positions=(),
        pending_orders=(),
    )
    eng._judge(snap)
    verdict = eng._verdicts[TID_B]
    gate = eng._gate(TID_B, snap)
    assert gate is not None and not gate.needs_stop, "a warning never declines the trade"

    bare = Action(
        kind=ActionKind.OPEN,
        terminal_id=TID_B,
        master_ticket=1,
        master_symbol="EURUSD",
        slave_symbol="EURUSD",
        type=PositionType.BUY,
        master_volume=1.0,
        entry=1.1,
        sl=0.0,
    )
    note = eng._stop_note(bare, cfg.terminals[TID_B], verdict)
    assert note is not None
    assert (note.rule, note.action, note.rule_id) == ("Stop loss required", "warned", "st")
    assert "no stop loss" in note.text()

    assert eng._stop_note(replace(bare, sl=1.09), cfg.terminals[TID_B], verdict) is None
    closing = replace(bare, kind=ActionKind.CLOSE)
    assert eng._stop_note(closing, cfg.terminals[TID_B], verdict) is None


def test_a_moving_loss_is_announced_once(
    cfg: AppConfig, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from mt5copier.guards import AccountGuards, GuardRule, GuardsDoc

    doc = GuardsDoc(
        accounts={
            "7@Prop": AccountGuards(
                initial_balance=10_000.0,
                rules=(
                    GuardRule(type="max_drawdown", params={"limit": 500, "unit": "money"}),
                    GuardRule(
                        type="max_drawdown",
                        params={"limit": 400, "unit": "money", "on_breach": "warn"},
                    ),
                ),
            )
        }
    )
    eng = Engine(cfg, guards=doc, guard_state_path=tmp_path / "state.json")
    snap = Snapshot(
        terminal_id=TID_B,
        state=WorkerState.CONNECTED,
        login=7,
        server="Prop",
        balance=10_000.0,
        equity=9_550.0,
        open_positions=(),
        pending_orders=(),
    )
    with caplog.at_level(logging.WARNING, logger="mt5copier.engine"):
        for equity in (9_550.0, 9_540.0, 9_520.0, 9_545.0):
            eng._judge(replace(snap, equity=equity))
    said = [r.getMessage() for r in caplog.records]
    assert sum("takes no new trades" in m for m in said) == 1
    assert sum(m.startswith("guards: Beta: ") for m in said) == 1


# --- cross-profile claims -------------------------------------------------------------------


def test_a_claim_held_elsewhere_refuses_the_spawn_and_says_who(
    cfg: AppConfig, tmp_path: Path
) -> None:
    """The real ``_spawn_worker``: the refusal returns before any process is created."""
    from mt5copier.claims import ClaimBook

    other = ClaimBook(tmp_path / "claims", "funded")
    other.acquire([f"terminal:{TID_A}"])
    try:
        eng = Engine(cfg, claims=ClaimBook(tmp_path / "claims", "scalp"))
        eng._spawn_worker(cfg.terminals[TID_A])
        assert TID_A not in eng._workers, "no worker may exist for a refused terminal"
        view = next(v for v in eng.views() if v.cfg.terminal_id == TID_A)
        assert view.claim_refused is not None
        assert "funded" in view.claim_refused
        assert view.status(1.0) == view.claim_refused, "the row's status is the refusal"
    finally:
        other.release_all()


def test_reaping_a_worker_releases_its_claims(cfg: AppConfig, tmp_path: Path) -> None:
    from mt5copier.claims import ClaimBook

    mine = ClaimBook(tmp_path / "claims", "scalp")
    eng = Engine(cfg, claims=mine)
    mine.acquire([f"terminal:{TID_A}"])
    eng._claimed[TID_A] = (f"terminal:{TID_A}",)
    eng._workers[TID_A] = FakeWorker(TID_A)  # type: ignore[assignment]

    eng._reap(TID_A)

    other = ClaimBook(tmp_path / "claims", "funded")
    other.acquire([f"terminal:{TID_A}"])  # must not raise: the reap let go
    other.release_all()


def test_unassigning_a_refused_terminal_drops_the_stale_note(
    cfg: AppConfig, engine: Engine
) -> None:
    """The fixture engine spawns nothing real, so only the note's lifecycle is on trial."""
    engine._claim_refused[TID_A] = "the terminal is in use by profile 'funded'"
    engine.config.terminals[TID_A].role = Role.UNASSIGNED
    engine.sync()
    view = next(v for v in engine.views() if v.cfg.terminal_id == TID_A)
    assert view.claim_refused is None


def test_the_retry_tick_spawns_a_refused_terminal_once_the_claim_is_free(
    engine: Engine,
) -> None:
    """Closing the other profile's window frees its locks with its process; the drain
    loop's tick must then bring the terminal up without anyone pressing Rescan."""
    engine._claim_refused[TID_A] = "the terminal is in use by profile 'funded'"
    engine._retry_refused_claims()
    assert engine.spawned == [TID_A]  # type: ignore[attr-defined]
    assert engine._claims_retry_at > 0, "the tick paces itself"
    # The fixture's fake spawn does not clear the note (the real one does); the next
    # tick sees the worker standing and drops it.
    engine._claims_retry_at = 0.0
    engine._retry_refused_claims()
    assert TID_A not in engine._claim_refused


def test_the_retry_tick_waits_between_tries_and_skips_the_exeless(engine: Engine) -> None:
    engine._claim_refused[TID_NOEXE] = "the account is in use by profile 'funded'"
    engine._retry_refused_claims()
    assert engine.spawned == [], "no executable: nothing to bring up"  # type: ignore[attr-defined]
    engine._claim_refused[TID_A] = "the terminal is in use by profile 'funded'"
    engine._retry_refused_claims()  # inside the pacing window from the first call
    assert engine.spawned == [], "a tick within the pause does nothing"  # type: ignore[attr-defined]


def test_the_retry_tick_keeps_the_note_while_the_claim_is_still_held(
    cfg: AppConfig, tmp_path: Path
) -> None:
    """The real ``_spawn_worker`` against a live lock: the note survives the retry."""
    from mt5copier.claims import ClaimBook

    other = ClaimBook(tmp_path / "claims", "funded")
    other.acquire([f"terminal:{TID_A}"])
    try:
        eng = Engine(cfg, claims=ClaimBook(tmp_path / "claims", "scalp"))
        eng._spawn_worker(cfg.terminals[TID_A])
        eng._retry_refused_claims()
        assert TID_A not in eng._workers
        view = next(v for v in eng.views() if v.cfg.terminal_id == TID_A)
        assert view.claim_refused is not None and "funded" in view.claim_refused
    finally:
        other.release_all()
