"""Supervisor tests.

``_spawn_worker`` is stubbed in the reconciliation tests so nothing is actually forked; the
drain-loop tests use the real ``multiprocessing.Queue`` but feed it from this process, which
exercises the ordering and error-retention logic without a child.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.engine import Engine, TerminalView
from mt5copier.protocol import ActionKind, OrderResult, Snapshot, WorkerState

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
        eng._workers[tcfg.terminal_id] = _FakeWorker(tcfg.terminal_id)  # type: ignore[assignment]
        eng._received_at[tcfg.terminal_id] = time.monotonic()

    monkeypatch.setattr(eng, "_spawn_worker", fake_spawn)
    eng.spawned = spawned  # type: ignore[attr-defined]
    return eng


class _FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.alive = False

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False


class _FakeQueue:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def put_nowait(self, item: object) -> None:
        self.sent.append(item)

    def close(self) -> None:
        pass


class _FakeWorker:
    def __init__(self, terminal_id: str) -> None:
        self.terminal_id = terminal_id
        self.process = _FakeProcess()
        self.cmd_q = _FakeQueue()
        self.started_at = time.monotonic()


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

    class Stubborn(_FakeProcess):
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
