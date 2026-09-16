"""Supervisor: owns one worker process per designated terminal and aggregates their state.

Deployment shape for Stage 1: the engine runs inside the UI process on a background thread,
and workers are subprocesses. ``zele-mt5`` put a WebSocket IPC server between its engine and
any consumer, which is the right call once there are several consumers (it grew a web dashboard
that used exactly that seam) -- but Stage 1 has precisely one consumer, the Textual app, so the
server would be unused indirection. The public surface here is deliberately narrow and
serialisable-friendly (:meth:`views` returns plain data), so a future IPC layer can wrap the
engine without reaching into it.

The engine process must never import MetaTrader5. It imports :mod:`worker` only to reference
``run_worker`` as a spawn target; the extension is loaded lazily inside the child.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import AppConfig, Role, TerminalConfig
from .logging_setup import get_logger
from .paths import dryrun_state_path, log_dir
from .protocol import Command, CommandKind, Snapshot, WorkerState
from .reconcile import Action, DryRunLedger, SlaveLink
from .worker import run_worker

log = get_logger(__name__)

_STOP_JOIN_TIMEOUT_S = 5.0

_ACTION_HISTORY = 500
"""How many intended actions to keep for the UI. Bounded because a busy master could otherwise
grow this without limit over a long session."""


@dataclass(frozen=True, slots=True)
class RecordedAction:
    """An intended action plus when the engine derived it.

    The timestamp is kept outside :class:`Action` on purpose: actions are compared by value to
    tell a new decision from the same one re-derived next tick, and a clock reading would make
    every one of them unique.
    """

    ts: float
    action: Action


@dataclass
class TerminalView:
    """Everything the UI needs for one row: config, latest snapshot, and liveness."""

    cfg: TerminalConfig
    snapshot: Snapshot | None = None
    received_at: float | None = None
    process_alive: bool = False
    supervised: bool = False
    """Whether the engine currently has a worker for this terminal."""

    def is_stale(self, timeout_s: float) -> bool:
        """True when a supervised worker has stopped reporting.

        Distinct from DISCONNECTED: a disconnected terminal still sends snapshots, so it is the
        broker link that is down. Staleness means our own process has gone quiet, which is a
        different failure with a different fix (restart the worker, not the terminal).
        """
        if not self.supervised or self.received_at is None:
            return False
        return (time.monotonic() - self.received_at) > timeout_s

    def status(self, timeout_s: float) -> str:
        """Short status string for display."""
        if not self.supervised:
            return "not connected" if self.cfg.role is not Role.UNASSIGNED else "-"
        if not self.process_alive:
            return "worker dead"
        if self.is_stale(timeout_s):
            return "stale"
        if self.snapshot is None:
            return "starting"
        return str(self.snapshot.state)


@dataclass
class _Worker:
    terminal_id: str
    process: mp.process.BaseProcess
    cmd_q: mp.Queue
    started_at: float = field(default_factory=time.monotonic)


class Engine:
    """Spawns, supervises and aggregates per-terminal workers.

    Thread-safety: :meth:`views` and the mutating methods are guarded by one lock. The drain
    thread is the only writer of snapshot state.
    """

    def __init__(self, cfg: AppConfig, state_path: Path | None = None) -> None:
        # Windows only offers the spawn start method; request it explicitly so behaviour is
        # identical if this ever runs on Linux for testing.
        self._ctx = mp.get_context("spawn")
        self._cfg = cfg
        self._out_q: mp.Queue = self._ctx.Queue(maxsize=10_000)
        self._workers: dict[str, _Worker] = {}
        self._snapshots: dict[str, Snapshot] = {}
        self._received_at: dict[str, float] = {}
        self._lock = threading.RLock()
        self._drain_thread: threading.Thread | None = None
        self._stop_drain = threading.Event()

        # --- Stage 2a: detection ------------------------------------------------------------
        self._ledger = DryRunLedger()
        self._actions: deque[RecordedAction] = deque(maxlen=_ACTION_HISTORY)
        self._action_total = 0
        self._state_path = state_path if state_path is not None else dryrun_state_path()
        self._warned_live = False

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        """Start the drain thread and bring workers in line with config."""
        self._load_state()
        if self._drain_thread is None:
            self._stop_drain.clear()
            self._drain_thread = threading.Thread(
                target=self._drain_loop, name="engine-drain", daemon=True
            )
            self._drain_thread.start()
        self.sync()

    def stop(self) -> None:
        """Stop every worker and the drain thread. Safe to call more than once."""
        with self._lock:
            terminal_ids = list(self._workers)
        for tid in terminal_ids:
            self._stop_worker(tid)
        self._stop_drain.set()
        thread = self._drain_thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._drain_thread = None

    def __enter__(self) -> Engine:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # --- worker management ------------------------------------------------------------------

    def sync(self) -> None:
        """Reconcile running workers against the set of assigned, usable terminals.

        Called after any role change. Spawning is skipped for terminals with no executable --
        an uninstalled terminal is shown in the UI but never connected to.
        """
        with self._lock:
            wanted: dict[str, TerminalConfig] = {}
            for tcfg in self._cfg.assigned():
                if tcfg.exe_path is None or not Path(tcfg.exe_path).is_file():
                    log.warning(
                        "skipping %s (%s): no terminal executable at %s",
                        tcfg.label,
                        tcfg.terminal_id[:8],
                        tcfg.exe_path,
                    )
                    continue
                wanted[tcfg.terminal_id] = tcfg

            for tid in list(self._workers):
                if tid not in wanted:
                    self._stop_worker(tid)

            for tid, tcfg in wanted.items():
                existing = self._workers.get(tid)
                if existing is not None and existing.process.is_alive():
                    continue
                if existing is not None:
                    log.warning("worker for %s died; respawning", tid[:8])
                    self._reap(tid)
                self._spawn_worker(tcfg)

    def restart(self, terminal_id: str) -> None:
        """Force a full worker restart (as opposed to an in-worker reconnect)."""
        with self._lock:
            tcfg = self._cfg.terminals.get(terminal_id)
            self._stop_worker(terminal_id)
            if tcfg is not None and tcfg.enabled and tcfg.role is not Role.UNASSIGNED:
                if tcfg.exe_path is not None and Path(tcfg.exe_path).is_file():
                    self._spawn_worker(tcfg)

    def reconnect(self, terminal_id: str) -> None:
        """Ask a live worker to drop and re-establish its MT5 session."""
        with self._lock:
            worker = self._workers.get(terminal_id)
            if worker is None:
                return
            try:
                worker.cmd_q.put_nowait(Command(kind=CommandKind.RECONNECT))
            except Exception as exc:
                log.debug("could not send reconnect to %s: %r", terminal_id[:8], exc)

    def _spawn_worker(self, tcfg: TerminalConfig) -> None:
        cmd_q: mp.Queue = self._ctx.Queue(maxsize=64)
        log_file = log_dir() / f"worker-{tcfg.terminal_id[:8]}.log"
        process = self._ctx.Process(
            target=run_worker,
            name=f"mt5-{tcfg.terminal_id[:8]}",
            args=(
                tcfg.terminal_id,
                str(tcfg.exe_path),
                self._out_q,
                cmd_q,
                str(log_file),
                self._cfg.poll_interval_ms,
                list(self._cfg.reconnect_backoff_s),
            ),
            daemon=True,
        )
        process.start()
        self._workers[tcfg.terminal_id] = _Worker(tcfg.terminal_id, process, cmd_q)
        self._received_at[tcfg.terminal_id] = time.monotonic()
        log.info("spawned worker pid=%s for %s", process.pid, tcfg.label)

    def _stop_worker(self, terminal_id: str) -> None:
        """Graceful stop, escalating to terminate.

        Mirrors the shutdown protocol from ``zele-mt5``'s PluginManager: send a stop command,
        join with a timeout, then terminate. The join matters because the worker needs a moment
        to call ``mt5.shutdown()``; killing it outright can leave the terminal's IPC endpoint
        briefly unusable for the next connection.
        """
        worker = self._workers.get(terminal_id)
        if worker is None:
            return
        log.info("stopping worker for %s", terminal_id[:8])
        try:
            worker.cmd_q.put_nowait(Command(kind=CommandKind.STOP))
        except Exception:
            pass
        worker.process.join(timeout=_STOP_JOIN_TIMEOUT_S)
        if worker.process.is_alive():
            log.warning("worker %s did not exit; terminating", terminal_id[:8])
            worker.process.terminate()
            worker.process.join(timeout=2.0)
        self._reap(terminal_id)

    def _reap(self, terminal_id: str) -> None:
        worker = self._workers.pop(terminal_id, None)
        if worker is not None:
            try:
                worker.cmd_q.close()
            except Exception:
                pass
        self._snapshots.pop(terminal_id, None)
        self._received_at.pop(terminal_id, None)

    # --- state ------------------------------------------------------------------------------

    def _drain_loop(self) -> None:
        """Move snapshots off the shared queue into the latest-wins state map."""
        while not self._stop_drain.is_set():
            try:
                snap = self._out_q.get(timeout=0.2)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                break
            if not isinstance(snap, Snapshot):
                continue
            with self._lock:
                current = self._snapshots.get(snap.terminal_id)
                # Out-of-order delivery would otherwise let a stale CONNECTING overwrite a
                # newer CONNECTED, making the UI flicker between states.
                if current is not None and snap.seq < current.seq:
                    continue
                # Keep the last error visible after recovery, so a transient failure leaves a
                # trace in the UI rather than vanishing.
                if snap.error is None and current is not None and current.error is not None:
                    if snap.state is WorkerState.CONNECTED:
                        # dataclasses.replace, not **__dict__: Snapshot is slots=True.
                        snap = replace(snap, error=current.error)
                self._snapshots[snap.terminal_id] = snap
                self._received_at[snap.terminal_id] = time.monotonic()
                self._reconcile(snap)

    # --- Stage 2a: detection ----------------------------------------------------------------

    def _reconcile(self, snap: Snapshot) -> None:
        """Diff the master's positions and record what *would* be copied.

        Runs on the drain thread, under the engine lock, driven by the master's own snapshots.
        That is the natural clock: reconciliation cannot run more often than the master reports,
        and the work is pure and cheap (a handful of dict lookups per slave per tick).

        Every guard below is a refusal to act on an incomplete picture. The expensive mistake
        available here is deriving "the master is flat" from a failed read, which would propose
        closing every copied position at once.
        """
        if not self._cfg.copying_enabled:
            return
        master = self._cfg.master
        if master is None or snap.terminal_id != master.terminal_id:
            return
        if snap.state is not WorkerState.CONNECTED:
            return
        if snap.open_positions is None:
            log.debug("master positions unreadable this tick; not reconciling")
            return
        if snap.pending_orders is None:
            # Both lists or neither. Without the orders we cannot tell a cancelled order from
            # a filled one, and guessing wrong either drops a trade or cancels a live copy.
            log.debug("master orders unreadable this tick; not reconciling")
            return

        if not self._cfg.dry_run and not self._warned_live:
            # Stage 2a has no execution code. Pretending otherwise -- or quietly continuing as
            # though dry_run were set -- is how a copier ends up trusted before it works.
            self._warned_live = True
            log.error(
                "dry_run is false but no execution exists in stage 2a; "
                "detecting only and sending nothing"
            )

        links = self._slave_links()
        if not links:
            return

        try:
            actions = self._ledger.step(
                snap.open_positions,
                links,
                master_orders=snap.pending_orders,
                now=time.monotonic(),
                grace_seconds=self._cfg.join_grace_seconds,
            )
        except Exception:
            # A crash in the diff must not kill the drain thread, which every terminal's
            # liveness display depends on.
            log.exception("reconciliation failed; skipping this tick")
            return

        if not actions:
            return

        now = time.time()
        labels = {link.cfg.terminal_id: link.cfg.label for link in links}
        for action in actions:
            self._actions.append(RecordedAction(ts=now, action=action))
            self._action_total += 1
            log.info(
                "[dry-run] %s: %s",
                labels.get(action.terminal_id, action.terminal_id[:8]),
                action.describe(),
            )
        self._save_state()

    def _slave_links(self) -> list[SlaveLink]:
        """Each enabled slave, paired with whether its terminal is reachable right now.

        A slave is only live if its worker is reporting CONNECTED *and* those reports are
        fresh. Stale-but-CONNECTED is the trap: the last snapshot says the link was fine, and
        if that were trusted, a wedged worker would look copyable forever. Copying into a
        terminal we are no longer hearing from is exactly the failure this whole design is
        built to avoid.

        Caller must hold the lock.
        """
        timeout = self._cfg.heartbeat_timeout_s
        monotonic_now = time.monotonic()
        links: list[SlaveLink] = []
        for cfg in self._cfg.slaves:
            if not cfg.enabled:
                continue
            snap = self._snapshots.get(cfg.terminal_id)
            received_at = self._received_at.get(cfg.terminal_id)
            fresh = received_at is not None and (monotonic_now - received_at) <= timeout
            connected = snap is not None and snap.state is WorkerState.CONNECTED and fresh
            links.append(SlaveLink(cfg=cfg, connected=connected))
        return links

    def recent_actions(self, limit: int | None = None) -> list[RecordedAction]:
        """Most recent intended actions, oldest first."""
        with self._lock:
            items = list(self._actions)
        return items if limit is None else items[-limit:]

    @property
    def action_total(self) -> int:
        """Actions derived since this engine started, including ones dropped from history."""
        with self._lock:
            return self._action_total

    def believed_copies(self) -> int:
        with self._lock:
            return len(self._ledger.believed())

    def clear_detection_state(self) -> None:
        """Forget every believed copy and the action history.

        Needed when the dry run is restarted against a different master, where the old beliefs
        describe positions that were never ours.
        """
        with self._lock:
            self._ledger = DryRunLedger()
            self._actions.clear()
            self._save_state()

    def _load_state(self) -> None:
        with self._lock:
            if not self._state_path.exists():
                return
            try:
                raw = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._ledger.load_json(raw)
            except (OSError, ValueError) as exc:
                # Bookkeeping, not settings: starting over is safe, and refusing to start is
                # not. The cost of a lost ledger is one re-proposed action per open position.
                log.warning("could not read dry-run state from %s: %s", self._state_path, exc)
                return
            log.info(
                "restored %d believed copies from %s",
                len(self._ledger.believed()),
                self._state_path,
            )

    def _save_state(self) -> None:
        """Persist believed copies, atomically, so a restart proposes nothing spurious."""
        payload = json.dumps(self._ledger.to_json(), indent=2) + "\n"
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._state_path.parent), prefix=self._state_path.name + ".", suffix=".tmp"
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
                os.replace(tmp, self._state_path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.warning("could not write dry-run state to %s: %s", self._state_path, exc)

    def views(self) -> list[TerminalView]:
        """Snapshot of all known terminals, ordered by label, for rendering.

        Returns copies, so the caller can hold them across the event loop without racing the
        drain thread.
        """
        with self._lock:
            out: list[TerminalView] = []
            for tcfg in self._cfg.terminals.values():
                worker = self._workers.get(tcfg.terminal_id)
                out.append(
                    TerminalView(
                        cfg=tcfg.model_copy(deep=True),
                        snapshot=self._snapshots.get(tcfg.terminal_id),
                        received_at=self._received_at.get(tcfg.terminal_id),
                        process_alive=bool(worker and worker.process.is_alive()),
                        supervised=worker is not None,
                    )
                )
        out.sort(key=lambda v: (v.cfg.label.lower(), v.cfg.terminal_id))
        return out

    @property
    def config(self) -> AppConfig:
        return self._cfg

    def health(self) -> dict[str, int]:
        """Counts for the header line."""
        views = self.views()
        timeout = self._cfg.heartbeat_timeout_s
        return {
            "total": len(views),
            "supervised": sum(1 for v in views if v.supervised),
            "live": sum(1 for v in views if v.snapshot is not None and v.snapshot.is_live),
            "stale": sum(1 for v in views if v.is_stale(timeout)),
        }
