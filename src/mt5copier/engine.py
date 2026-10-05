"""Supervisor: owns one worker process per designated terminal and aggregates their state.

Deployment shape for Stage 1: the engine runs inside the UI process on a background thread,
and workers are subprocesses. ``zele-mt5`` put a WebSocket IPC server between its engine and
any consumer, which is the right call once there are several consumers (it grew a web dashboard
that used exactly that seam; this project tried one and dropped it) -- but Stage 1 has precisely one consumer, the Textual app, so the
server would be unused indirection. The public surface here is deliberately narrow and
serialisable-friendly (:meth:`views` returns plain data), so a future IPC layer can wrap the
engine without reaching into it.

The engine process must never import MetaTrader5. It imports :mod:`worker` only to reference
``run_worker`` as a spawn target; the extension is loaded lazily inside the child.
"""

from __future__ import annotations

import itertools
import multiprocessing as mp
import queue
import threading
import time
from collections import deque
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import role_actions
from . import setups as setups_mod
from .breakeven import breakeven_level, buffer_distance, check_breakeven_price
from .config import (
    BREAKEVEN_FIELDS,
    AppConfig,
    BreakevenMode,
    Role,
    SizingMode,
    TerminalClosing,
    TerminalConfig,
    account_key,
)
from .copy_stats import StatsBook, TerminalStats
from .day_book import DayBook, MasterEvent, Outcome, SlaveOutcome
from .guards import NO_VERDICT, GuardBook, GuardReport, GuardsDoc, Verdict
from .history import HistoryStore, Trade, step_for
from .logging_setup import get_logger
from .news import NewsCalendar
from .paths import dryrun_store_path, history_db_path, log_dir, mapping_db_path
from .protocol import (
    COPIER_MAGIC,
    DISPATCHABLE_KINDS,
    ActionKind,
    Command,
    CommandKind,
    OrderCommand,
    OrderResult,
    Snapshot,
    WorkerState,
)
from .reconcile import Action, CopiedPosition, CopyLedger, GuardGate, OpenCopy, SlaveLink
from .settings_edit import copies_word
from .setups import Setup
from .sizing import risk_budget
from .store import MappingStore, Orphan, find_missing, find_missing_orders, find_orphans
from .worker import run_worker

log = get_logger(__name__)

_STOP_JOIN_TIMEOUT_S = 5.0

_ACTION_HISTORY = 500
"""How many intended actions to keep for the UI. Bounded because a busy master could otherwise
grow this without limit over a long session."""

_RESULT_HISTORY = 200

_PENDING_CMD_TTL_S = 120.0
"""How long the engine remembers which command_id it last sent for a key, so a stale answer
can be recognised and refused. Comfortably longer than the ledger's in-flight timeout, since
the whole point is to still know about a command the ledger has already given up on."""


@dataclass(frozen=True, slots=True)
class RecordedAction:
    """An intended action plus when the engine derived it.

    The timestamp is kept outside :class:`Action` on purpose: actions are compared by value to
    tell a new decision from the same one re-derived next tick, and a clock reading would make
    every one of them unique.
    """

    ts: float
    action: Action


@dataclass(frozen=True, slots=True)
class RecordedResult:
    """What the broker said, plus when we heard it."""

    ts: float
    result: OrderResult


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


_BE_RETRY_FACTOR = 1.5
"""After a refused breakeven, how much more room than the refused attempt the price needs
before the next try. Each try is at a strictly better distance, so a broker that keeps
refusing is asked a handful of times as the trade runs, never once per tick."""


def _only_breakeven_changed(old: TerminalConfig, new: TerminalConfig) -> bool:
    """Whether an edit touches nothing but the breakeven settings (see ``BREAKEVEN_FIELDS``)."""
    changed = {
        name for name in TerminalConfig.model_fields if getattr(old, name) != getattr(new, name)
    }
    return changed <= BREAKEVEN_FIELDS


class Engine:
    """Spawns, supervises and aggregates per-terminal workers.

    Thread-safety: :meth:`views` and the mutating methods are guarded by one lock. The drain
    thread is the only writer of snapshot state.
    """

    def __init__(
        self,
        cfg: AppConfig,
        state_path: Path | None = None,
        mapping_path: Path | None = None,
        guards: GuardsDoc | None = None,
        guard_state_path: Path | None = None,
        history_path: Path | None = None,
        calendar: NewsCalendar | None = None,
    ) -> None:
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

        # --- detection and copying ----------------------------------------------------------
        self._actions: deque[RecordedAction] = deque(maxlen=_ACTION_HISTORY)
        self._action_total = 0
        self._blocked_reason: str | None = None

        # --- startup reconciliation and per-slave holds --------------------------------------
        self._reconciled: set[str] = set()
        """Slaves matched against the belief store since this store was opened. A slave outside
        this set has not been reconciled yet and must not be copied to."""
        self._orphans: dict[str, list[Orphan]] = {}
        self._orphan_ack: dict[str, set[int]] = {}
        """Slave tickets the user has signed off. Keyed per ticket rather than per terminal so
        a *new* orphan appearing later re-arms the hold instead of inheriting the old consent."""
        self._hold_reason: dict[str, str] = {}
        self._warned_dry_missing: set[str] = set()

        # --- dispatch ------------------------------------------------------------------------
        self._command_ids = itertools.count(1)
        self._pending_cmd: dict[tuple[str, int, ActionKind], tuple[int, float]] = {}
        """The command_id last sent per (slave, master ticket, op), so an answer to a command
        we have already given up on can be recognised and refused instead of applied."""
        self._results: deque[RecordedResult] = deque(maxlen=_RESULT_HISTORY)
        self._failed_total = 0
        self._warned_undispatchable: set[ActionKind] = set()
        self._be_room: dict[tuple[str, int], float] = {}
        """Per copy: how far the price was from the breakeven stop when it was last set. Kept
        so that if the broker refuses the stop, the retry can wait for more room than that."""
        self._be_unpriced: set[tuple[str, int]] = set()
        """Copies already reported as unable to go to breakeven because their commission
        cannot be priced, so it is said once rather than every tick."""
        self._be_min_room: dict[tuple[str, int], float] = {}
        """Per copy: the room the price must have from the breakeven stop before trying again,
        after a refusal. Session only: after a restart the first attempt is made as usual."""
        self._stats = StatsBook()
        """Copy-health numbers for the detail page. Display only; nothing here decides."""
        self._day = DayBook()
        """Each master trade with what became of it on every slave, for the map's day
        book. Display only, like the statistics."""
        self._history = HistoryStore(
            history_path if history_path is not None else history_db_path(),
            keep_days=cfg.history_days,
        )
        """Every step taken with each trade on each slave, kept for days. Fed by the day book
        as each outcome settles; display only."""
        self._day.listener = self._on_step
        self._calendar = calendar if calendar is not None else NewsCalendar()
        """The news calendar: fetched on its own thread, and only once a news rule asks."""
        self._guards = GuardBook(
            guards if guards is not None else GuardsDoc(), guard_state_path, self._calendar
        )
        """Each account's prop-firm rules and how they stand."""
        self._verdicts: dict[str, Verdict] = {}
        """Per slave: what its guards said on its latest snapshot. Read when the master's tick
        derives new trades for it, and when a trade is sent."""

        self._dryrun_path = state_path if state_path is not None else dryrun_store_path()
        self._mapping_path = mapping_path if mapping_path is not None else mapping_db_path()
        self._store: MappingStore | None = None
        self._store_is_dry: bool | None = None
        """Which mode the open store belongs to, so a change of ``dry_run`` swaps it rather than
        carrying simulated beliefs into a live session."""
        self._ledger = CopyLedger()

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        """Start the drain thread and bring workers in line with config."""
        if self._drain_thread is None:
            self._stop_drain.clear()
            self._drain_thread = threading.Thread(
                target=self._drain_loop, name="engine-drain", daemon=True
            )
            self._drain_thread.start()
        self._calendar.start()
        self.sync()

    def stop(self) -> None:
        """Stop every worker and the drain thread. Safe to call more than once.

        Closes the terminals the ``close_terminals`` setting says to: by default every one this
        engine launched, symmetrically with how starting a worker can launch one.
        """
        with self._lock:
            terminal_ids = list(self._workers)
        for tid in terminal_ids:
            self._stop_worker(tid, close_terminal=True)
        self._stop_drain.set()
        self._calendar.stop()
        thread = self._drain_thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._drain_thread = None
        self._history.close()

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
                    # Unassigned (or newly unusable) for good, not a transient restart -- so if
                    # this terminal is one we launched, we are done with it.
                    self._stop_worker(tid, close_terminal=True)

            for tid, tcfg in wanted.items():
                existing = self._workers.get(tid)
                if existing is not None and existing.process.is_alive():
                    continue
                if existing is not None:
                    log.warning("worker for %s died; respawning", tid[:8])
                    self._reap(tid)
                self._spawn_worker(tcfg)

    def restart(self, terminal_id: str) -> None:
        """Force a full worker restart (as opposed to an in-worker reconnect).

        Never closes the terminal, even one we launched: a restart wants the same terminal
        back a moment later, not a bounced window.
        """
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

    def _stop_worker(self, terminal_id: str, *, close_terminal: bool = False) -> None:
        """Graceful stop, escalating to terminate.

        Mirrors the shutdown protocol from ``zele-mt5``'s PluginManager: send a stop command,
        join with a timeout, then terminate. The join matters because the worker needs a moment
        to call ``mt5.shutdown()``; killing it outright can leave the terminal's IPC endpoint
        briefly unusable for the next connection -- and, with ``close_terminal`` set, skips the
        worker's own chance to close the terminal it launched, which only runs on a graceful
        exit. That is an accepted gap, not a new one: a forced kill already skips the clean
        ``mt5.shutdown()`` the same way.

        ``close_terminal`` says we are done with this terminal for good -- stopping the app, or
        the terminal being unassigned -- never a restart, which wants it straight back. What is
        then closed is the ``close_terminals`` setting's call: nothing, only a terminal the
        worker launched (see :mod:`mt5copier.worker`), or whichever is running.
        """
        worker = self._workers.get(terminal_id)
        if worker is None:
            return
        closing = self._cfg.close_terminals
        close = close_terminal and closing is not TerminalClosing.NEVER
        close_any = close and closing is TerminalClosing.ALL
        log.info(
            "stopping worker for %s (close_terminal=%s, close_any=%s)",
            terminal_id[:8],
            close,
            close_any,
        )
        try:
            worker.cmd_q.put_nowait(
                Command(kind=CommandKind.STOP, close_terminal=close, close_any=close_any)
            )
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
        self._stats.on_worker_gone(terminal_id)
        # A worker that died or restarted may have missed closes, so its slave has to be
        # matched against the store again before anything is derived for it.
        self._forget_reconciliation(terminal_id)

    def _forget_reconciliation(self, terminal_id: str) -> None:
        """Drop what we knew about this slave's agreement with the store.

        Not the beliefs themselves -- those are the store's and describe real positions. Only
        the conclusion that we had checked them, which is what expires.
        """
        self._reconciled.discard(terminal_id)
        self._orphans.pop(terminal_id, None)
        self._orphan_ack.pop(terminal_id, None)
        self._hold_reason.pop(terminal_id, None)
        self._warned_dry_missing.discard(terminal_id)

    # --- state ------------------------------------------------------------------------------

    def _drain_loop(self) -> None:
        """Move snapshots and order results off the shared queue.

        One queue carries both, discriminated by type. Snapshots are latest-wins and droppable;
        a result is neither, because it is the only evidence that an order we sent actually
        happened.
        """
        while not self._stop_drain.is_set():
            try:
                snap = self._out_q.get(timeout=0.2)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                break
            if isinstance(snap, OrderResult):
                with self._lock:
                    self._on_result(snap)
                continue
            if not isinstance(snap, Snapshot):
                continue
            with self._lock:
                if not self._from_live_worker(snap):
                    continue
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
                self._stats.on_state(snap.terminal_id, snap.state, now=time.monotonic())
                self._guards.observe(account_key(snap.login, snap.server), snap, time.time())
                self._judge(snap)
                self._reconcile(snap)

    def _from_live_worker(self, snap: Snapshot) -> bool:
        """Whether ``snap`` came from the worker this terminal has *now*.

        A stopped worker's last snapshots can still be in the queue when it is reaped, and the
        drain files them afterwards. Kept, one would sit in ``_snapshots`` with the old worker's
        high ``seq`` -- and since the replacement counts from 0 again, the out-of-order check
        below would then refuse every snapshot it sent, leaving the terminal "stale" for good.
        That is what turning a terminal off and on again did. Unstamped snapshots are let
        through: only a worker stamps them, and it always does.
        """
        if snap.worker_pid is None:
            return True
        worker = self._workers.get(snap.terminal_id)
        return worker is not None and getattr(worker.process, "pid", None) == snap.worker_pid

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
            self._set_blocked("the master's positions cannot be read")
            return
        if snap.pending_orders is None:
            # Both lists or neither. Without the orders we cannot tell a cancelled order from
            # a filled one, and guessing wrong either drops a trade or cancels a live copy.
            self._set_blocked("the master's pending orders cannot be read")
            return
        self._set_blocked(None)

        # Before the links, not after: deciding whether a slave may be copied to now includes
        # reconciling it against the store, so the store has to exist first.
        self._ensure_store()
        links = self._slave_links()
        if not links:
            return

        for tid, ticket in self._ledger.expire_inflight():
            log.error("gave up waiting for #%s on %s", ticket, tid[:8])
            self._stats.on_expired(tid)
            self._day.on_expired(tid, ticket, ts=time.time())
            # If that order did reach the broker, a position exists that we have no record of.
            # Reconciling this slave again is what turns it into a reported orphan instead of
            # a silence -- which is exactly what the expiry message promises.
            self._forget_reconciliation(tid)
        self._sweep_pending()

        dry = self._cfg.dry_run
        if not dry:
            self._arm_breakevens(links)
        try:
            actions = self._ledger.step(
                snap.open_positions,
                links,
                master_orders=snap.pending_orders,
                now=time.monotonic(),
                grace_seconds=self._cfg.join_grace_seconds,
                # The one difference between rehearsal and the real thing: a dry run pretends
                # every order succeeded, live believes the result when it arrives.
                simulate=dry,
                dispatchable=DISPATCHABLE_KINDS,
            )
        except Exception:
            # A crash in the diff must not kill the drain thread, which every terminal's
            # liveness display depends on.
            log.exception("reconciliation failed; skipping this tick")
            return

        if not actions:
            return

        now = time.time()
        cfgs = {link.cfg.terminal_id: link.cfg for link in links}
        prefix = "[dry-run]" if dry else "[live]"
        self._day.begin_tick(now, snap.open_positions, snap.pending_orders)
        for action in actions:
            self._actions.append(RecordedAction(ts=now, action=action))
            self._action_total += 1
            if action.skip_reason is not None:
                self._stats.on_skipped(action.terminal_id, action.skip_reason.value)
            cfg = cfgs.get(action.terminal_id)
            log.info(
                "%s %s: %s",
                prefix,
                cfg.label if cfg is not None else action.terminal_id[:8],
                action.describe(),
            )
            invert = cfg is not None and cfg.invert

            if not action.executable:
                reason = action.skip_reason.value if action.skip_reason else None
                self._day.add(action, Outcome.SKIPPED, reason, invert=invert)
                continue
            if action.kind not in DISPATCHABLE_KINDS:
                self._warn_undispatchable(action.kind)
                self._day.add(action, Outcome.NOT_SENT, "this build cannot send it", invert=invert)
                continue
            if dry:
                self._day.add(action, Outcome.DRY_RUN, invert=invert)
                continue
            if cfg is None:
                continue
            slave_snap = self._snapshots.get(action.terminal_id)
            if slave_snap is None:
                self._day.add(
                    action, Outcome.NOT_SENT, "the slave has not reported yet", invert=invert
                )
                continue
            self._day.add(action, Outcome.SENDING, invert=invert)
            self._dispatch(action, cfg, slave_snap)
        self._day.end_tick(links, self._hold_reason)

    def _arm_breakevens(self, links: list[SlaveLink]) -> None:
        """Set a breakeven floor on every copy that has crossed its slave's trigger.

        Read off each slave's newest snapshot: the copy's own fill (``open_positions``) and its
        current price (``live_positions``). That makes this the one reader of the live payload
        outside the UI. It only decides *whether* to set a floor; the floor itself is belief,
        and belief is all the diff reads. A slave that is not connected, or whose positions
        could not be read, is left for a later tick -- the floor is set late, not wrongly.

        Live only: a dry run's copies do not exist on the slave, so there is nothing to measure.
        Caller must hold the lock.
        """
        for tid, master_ticket in self._ledger.take_refused_breakevens():
            key = (tid, master_ticket)
            tried = self._be_room.pop(key, 0.0)
            self._be_min_room[key] = max(tried * _BE_RETRY_FACTOR, self._be_min_room.get(key, 0.0))
            cfg = self._cfg.terminals.get(tid)
            log.warning(
                "%s: the broker refused the breakeven stop on #%s with the price %g away from "
                "it, likely too close for its minimum distance; it stays on the master's stop "
                "and breakeven is tried again once the price is %g away",
                (cfg.label if cfg is not None else "") or tid[:8],
                master_ticket,
                tried,
                self._be_min_room[key],
            )

        for link in links:
            cfg = link.cfg
            copies = self._ledger.believed(cfg.terminal_id)
            if not link.connected or not copies:
                continue
            if cfg.breakeven_mode is BreakevenMode.OFF and not any(c.breakeven_at for c in copies):
                continue
            snap = self._snapshots.get(cfg.terminal_id)
            if snap is None or snap.open_positions is None or snap.live_positions is None:
                continue
            opened = {p.ticket: p.price_open for p in snap.open_positions}
            current = {p.ticket: p.price_current for p in snap.live_positions}
            for copy in copies:
                ticket = copy.slave_ticket
                price = current.get(ticket) if ticket is not None else None
                if ticket is None or ticket not in opened or price is None:
                    continue
                self._warn_unpriced(cfg, copy)
                try:
                    level = breakeven_level(copy, opened[ticket], price, cfg)
                except Exception:
                    log.exception("breakeven check failed for #%s", copy.master_ticket)
                    continue
                if level is None:
                    continue
                key = (cfg.terminal_id, copy.master_ticket)
                room = abs(price - level)
                if room < self._be_min_room.get(key, 0.0):
                    continue
                self._be_room[key] = room
                self._ledger.set_breakeven(cfg.terminal_id, copy.master_ticket, level)
                self._history.note(
                    cfg.terminal_id,
                    copy.master_ticket,
                    copy.slave_symbol,
                    copy.type.name.lower(),
                    "breakeven",
                    f"reached at {price:g} (fill {opened[ticket]:g}): the stop moves to "
                    f"{level:g} and from now on only tightens",
                )
                log.info(
                    "%s: breakeven reached on #%s %s (fill %s, now %s); stop moves to %s and "
                    "from now on only tightens: the master's stop is copied only when tighter, "
                    "and a looser or removed one is ignored. Its target still follows the master",
                    cfg.label or cfg.terminal_id[:8],
                    ticket,
                    copy.slave_symbol,
                    opened[ticket],
                    price,
                    level,
                )

    def _warn_unpriced(self, cfg: TerminalConfig, copy: CopiedPosition) -> None:
        """Say once that a copy will never go to breakeven because its commission cannot be
        turned into a distance -- it was opened before that was recorded. Without this it
        would simply never fire, which is what the first live test looked like."""
        key = (cfg.terminal_id, copy.master_ticket)
        if key in self._be_unpriced or copy.breakeven_exempt or copy.breakeven_sl:
            return
        wanted = copy.breakeven_at or cfg.breakeven_mode is not BreakevenMode.OFF
        if not wanted or buffer_distance(copy, cfg) is not None:
            return
        self._be_unpriced.add(key)
        log.warning(
            "%s: #%s %s will not go to breakeven: it was opened before the copier recorded "
            "what its price moves are worth, so its commission cannot be turned into a stop "
            "distance. Copies opened from now on are not affected",
            cfg.label or cfg.terminal_id[:8],
            copy.slave_ticket,
            copy.slave_symbol,
        )

    def set_breakeven_exempt(self, terminal_id: str, master_ticket: int, exempt: bool) -> bool:
        """Exempt one copy from breakeven, or lift that. False if it is not a position we hold.

        Exempting a copy whose breakeven already fired drops the floor, so it follows the
        master's stop again from the next tick. Under the lock, so a tick never sees half of it.
        """
        with self._lock:
            done = self._ledger.set_breakeven_exempt(terminal_id, master_ticket, exempt)
            copy = self._ledger.get_position(terminal_id, master_ticket) if done else None
        if copy is not None:
            self._history.note(
                terminal_id,
                master_ticket,
                copy.slave_symbol,
                copy.type.name.lower(),
                "breakeven",
                "exempted by hand" if exempt else "re-enabled by hand",
            )
        if done:
            log.info(
                "#%s on %s: breakeven %s by hand",
                master_ticket,
                terminal_id[:8],
                "exempted" if exempt else "re-enabled",
            )
        return done

    def set_breakeven_at(self, terminal_id: str, master_ticket: int, price: float) -> str | None:
        """Set one copy's breakeven price, 0 to clear it. None means done; otherwise why not.

        Checked against the copy's own fill on the slave's newest snapshot, so a price on the
        wrong side of entry is refused here rather than turning into a stop the broker refuses.
        """
        with self._lock:
            copy = self._ledger.get_position(terminal_id, master_ticket)
            if copy is None:
                return "that position is not one of the copier's copies"
            if price:
                snap = self._snapshots.get(terminal_id)
                positions = (snap.open_positions if snap is not None else None) or ()
                fill = next(
                    (p.price_open for p in positions if p.ticket == copy.slave_ticket), None
                )
                if fill is None:
                    return "the copy's fill price cannot be read right now"
                problem = check_breakeven_price(copy, fill, price, self._cfg.terminals[terminal_id])
                if problem is not None:
                    return problem
            self._ledger.set_breakeven_at(terminal_id, master_ticket, price)
        self._history.note(
            terminal_id,
            master_ticket,
            copy.slave_symbol,
            copy.type.name.lower(),
            "breakeven",
            f"price set to {price:g} by hand" if price else "price cleared by hand",
        )
        log.info(
            "#%s on %s: breakeven price %s by hand",
            master_ticket,
            terminal_id[:8],
            f"set to {price:g}" if price else "cleared",
        )
        return None

    def _warn_undispatchable(self, kind: ActionKind) -> None:
        """Say once per session that we are deriving something this build cannot send.

        Without this the gap is silent: filtering the kind out keeps the log clean, and a
        clean log is indistinguishable from SL/TP sync working.
        """
        if kind in self._warned_undispatchable:
            return
        self._warned_undispatchable.add(kind)
        log.warning(
            "%s is being derived but is not in DISPATCHABLE_KINDS, so nothing will be sent "
            "for it and whatever it mirrors will not follow the master",
            kind,
        )

    def _dispatch(self, action: Action, cfg: TerminalConfig, snap: Snapshot) -> None:
        """Turn one action into an order command and put it on that slave's queue.

        The command is self-contained by design: the engine decides *what* and *how much
        money*, the worker decides *how many lots*, and the worker never reads config. That is
        why every per-slave setting is copied in here rather than looked up on the far side.
        """
        risk = self._risk_amount(cfg, snap)
        if risk is None:
            # _prepare_slave holds a slave that cannot be sized, so this should be
            # unreachable; if it ever is reached, refusing is still the safe answer.
            log.error(
                "not sending %s for #%s: %s has no risk budget",
                action.kind,
                action.master_ticket,
                cfg.label,
            )
            self._day.settle(action, Outcome.NOT_SENT, "its risk budget cannot be sized")
            return
        verdict = self._verdicts.get(cfg.terminal_id, NO_VERDICT)
        risk = self._capped_risk(action, cfg, risk, verdict)
        margin = verdict.max_margin
        opening = action.kind in (ActionKind.OPEN, ActionKind.PLACE)

        command_id = next(self._command_ids)
        cmd = OrderCommand(
            command_id=command_id,
            op=action.kind,
            master_ticket=action.master_ticket,
            master_symbol=action.master_symbol,
            direction=action.type,
            order_type=action.order_type,
            entry=action.entry,
            sl=action.sl,
            tp=action.tp,
            master_volume=action.master_volume,
            sizing_basis=action.sizing_basis,
            lot_multiple=action.lot_multiple,
            risk_amount=risk,
            commission_per_lot=cfg.commission_per_lot_round_turn,
            slippage_points=cfg.slippage_buffer_points,
            max_lot=cfg.max_lot,
            deviation_points=cfg.max_deviation_points,
            close_fraction=action.close_fraction,
            slave_ticket=action.slave_ticket,
            symbol_prefix=cfg.symbol_prefix,
            symbol_suffix=cfg.symbol_suffix,
            # Sorted so an unchanged map produces an identical command. The worker reads no
            # config at all, so a mapping not copied here has no effect on what is placed --
            # only on what the log calls it.
            symbol_map=tuple(sorted(cfg.symbol_map.items())),
            magic=COPIER_MAGIC,
            # Short on purpose: MT5 truncates comments, and the mapping table is the authority
            # on ownership. This is a hint for a human reading the terminal.
            comment=f"c{action.master_ticket}",
            allow_live=cfg.allow_live_trading,
            max_margin=margin.amount if opening and margin is not None else None,
            margin_cap=margin.mode if opening and margin is not None else "",
        )

        # Before sending, because an order that cannot be sent is answered on the spot.
        self._day.on_sent(action, command_id, sent_at=time.monotonic())
        worker = self._workers.get(cfg.terminal_id)
        if worker is None:
            self._abandon(cmd, cfg.terminal_id, "no worker is running for this terminal")
            return
        try:
            worker.cmd_q.put_nowait(cmd)
        except Exception as exc:
            self._abandon(
                cmd, cfg.terminal_id, f"its command queue would not accept the order: {exc!r}"
            )
            return

        sent_at = time.monotonic()
        self._pending_cmd[(action.terminal_id, action.master_ticket, action.kind)] = (
            command_id,
            sent_at,
        )
        self._stats.on_sent(
            command_id,
            sent_at=sent_at,
            op=action.kind,
            direction=action.type,
            master_entry=action.entry,
        )
        log.info("-> %s %s #%s (cmd %s)", cfg.label, action.kind, action.master_ticket, command_id)

    def _capped_risk(
        self, action: Action, cfg: TerminalConfig, risk: float, verdict: Verdict
    ) -> float:
        """The risk budget to size a new trade from, after its guards' risk cap.

        A cap set to decline never gets here: the slave's budget is the same for every trade
        sized from its stop, so the gate refused the trade already, where a dry run shows it
        too. Set to shrink, the budget comes down to the cap, which sizes the trade to fit; set
        to warn, it is said and the trade goes as sized.
        """
        cap = verdict.max_risk
        if cap is None or action.kind not in (ActionKind.OPEN, ActionKind.PLACE):
            return risk
        if risk <= cap.amount:
            return risk
        name = self._cfg.display_name(cfg.terminal_id)
        if cap.mode == "shrink":
            log.warning(
                "guards: #%s on %s sized to risk %.2f, not %.2f: %s",
                action.master_ticket,
                name,
                cap.amount,
                risk,
                cap.rule,
            )
            return cap.amount
        log.warning(
            "guards: #%s on %s risks %.2f, over the %.2f %s allows (set only to warn)",
            action.master_ticket,
            name,
            risk,
            cap.amount,
            cap.rule,
        )
        return risk

    def _abandon(self, cmd: OrderCommand, terminal_id: str, error: str) -> None:
        """Fail a command we could not even send, immediately.

        The alternative is leaving the key in flight for the ledger's full timeout while
        nothing is coming, which delays the same outcome by 30s and looks like a broker that
        went quiet. For an OPEN or a PLACE this is a *permanent* exclusion of that ticket,
        which is the honest reading of "we could not get the order out in time": by the time
        anything changed, the slave would be filling far from the master's entry.

        It is reported as transient all the same, because that describes what happened -- a
        worker that is missing now will be respawned. The ledger applies the policy, and for
        an open or a place the exclusion wins regardless; for a close or a modify, whose
        target state does not expire, the next tick derives it again.
        """
        log.error(
            "could not send %s for #%s to %s: %s",
            cmd.op,
            cmd.master_ticket,
            terminal_id[:8],
            error,
        )
        result = OrderResult(
            command_id=cmd.command_id,
            terminal_id=terminal_id,
            op=cmd.op,
            master_ticket=cmd.master_ticket,
            ok=False,
            retryable=True,
            error=error,
        )
        self._record_result(result)
        self._stats.on_abandoned(result)
        self._day.on_result(result, now=time.monotonic(), ts=time.time())
        self._ledger.apply_result(result)

    def _on_result(self, result: OrderResult) -> None:
        """Believe what the broker said, if it is answering the command we are waiting for.

        The correlation matters because the ledger keys belief on
        ``(slave, master ticket, op)`` and not on the command id. Without this check, a result
        that arrives after the ledger gave up -- and after the decision was re-derived and
        re-sent -- would be applied to the *new* command's key and describe the wrong order.

        Caller must hold the lock.
        """
        key = (result.terminal_id, result.master_ticket, result.op)
        expected = self._pending_cmd.get(key)
        if expected is None or expected[0] != result.command_id:
            self._record_result(result)
            self._stats.on_late(result.terminal_id)
            log.error(
                "dropping result for %s #%s on %s: it answers command %s, we are waiting on %s",
                result.op,
                result.master_ticket,
                result.terminal_id[:8],
                result.command_id,
                "nothing" if expected is None else expected[0],
            )
            return
        del self._pending_cmd[key]
        self._record_result(result)
        self._stats.on_result(result, now=time.monotonic())
        self._day.on_result(result, now=time.monotonic(), ts=time.time())
        if result.ok:
            log.info("%s", result.describe())
        else:
            log.error("%s", result.describe())
        self._ledger.apply_result(result)

    def _record_result(self, result: OrderResult) -> None:
        self._results.append(RecordedResult(ts=time.time(), result=result))
        if not result.ok:
            self._failed_total += 1

    def _sweep_pending(self) -> None:
        """Forget command ids too old to be answered.

        Age-based rather than driven by the ledger's expiry, because ``expire_inflight`` does
        not report which *op* it gave up on, and dropping every entry for a ticket would
        refuse a legitimate answer to a sibling command on the same position.
        """
        cutoff = time.monotonic() - _PENDING_CMD_TTL_S
        for key in [k for k, (_, sent_at) in self._pending_cmd.items() if sent_at < cutoff]:
            del self._pending_cmd[key]
        self._stats.forget_sent_before(cutoff)
        self._day.forget_sent_before(cutoff)

    def recent_results(self, limit: int | None = None) -> list[RecordedResult]:
        """Most recent order results, oldest first."""
        with self._lock:
            items = list(self._results)
        return items if limit is None else items[-limit:]

    def day_book(self, limit: int | None = None) -> list[MasterEvent]:
        """The master's most recent trades with each slave's outcome, oldest first."""
        with self._lock:
            return self._day.events(limit)

    def trade_history(self, terminal_id: str, *, since: float | None = None) -> list[Trade]:
        """The slave's trades with every step taken with each, the most recently active
        first. Read from disk, so it reaches back past this session."""
        return self._history.trades(terminal_id, since=since)

    def _on_step(self, head: MasterEvent, outcome: SlaveOutcome, action: Action | None) -> None:
        """The day book settled one slave's outcome: keep it in the trade's history. Called
        under the engine's lock; a failure here must never reach the copying."""
        try:
            self._history.keep_days = self._cfg.history_days
            self._history.record(step_for(head, outcome, action))
        except Exception:
            log.exception("could not record a step in the trade history")

    @property
    def day_book_since(self) -> float:
        """Wall-clock time from which :meth:`day_book` holds every master event."""
        with self._lock:
            return self._day.complete_since

    def terminal_stats(self, terminal_id: str) -> TerminalStats:
        """This terminal's copy-health numbers for the session, as a read-only copy."""
        with self._lock:
            return self._stats.snapshot(terminal_id, now=time.monotonic())

    def _judge(self, snap: Snapshot) -> None:
        """Ask a slave's guards what they say now, and announce it when that changes.

        Announced on the transition, like a hold: a slave quietly declining every trade is
        indistinguishable from a master that is not trading.
        """
        tid = snap.terminal_id
        cfg = self._cfg.terminals.get(tid)
        if cfg is None or cfg.role is not Role.SLAVE:
            self._verdicts.pop(tid, None)
            return
        risk = self._risk_amount(cfg, snap)
        verdict = self._guards.verdict(
            account_key(snap.login, snap.server), snap, time.time(), sizing_risk=risk
        )
        before = self._verdicts.get(tid, NO_VERDICT)
        self._verdicts[tid] = verdict
        name = self._cfg.display_name(tid)
        # The hold's text carries the live measurement, which moves with every tick of a
        # floating loss; only the slave being held back or let go is news.
        if (verdict.hold is None) != (before.hold is None):
            if verdict.hold is None:
                log.info("guards: %s takes new trades again (was: %s)", name, before.hold)
            else:
                log.warning("guards: %s takes no new trades: %s", name, verdict.hold)
        if verdict.news != before.news:
            if verdict.news:
                log.warning("guards: %s: %s", name, verdict.news_why)
            else:
                log.info("guards: %s: the news is over; new trades are copied again", name)
        for rule_id, line in zip(verdict.warned_by, verdict.warnings, strict=True):
            if rule_id not in before.warned_by:
                log.warning("guards: %s: %s", name, line)

    def _gate(self, terminal_id: str, snap: Snapshot | None) -> GuardGate | None:
        verdict = self._verdicts.get(terminal_id)
        if verdict is None or verdict == NO_VERDICT:
            return None
        cfg = self._cfg.terminals[terminal_id]
        risk = self._risk_amount(cfg, snap) if snap is not None else None
        cap = verdict.max_risk
        over = cap is not None and cap.mode == "decline" and risk is not None and risk > cap.amount
        return GuardGate(
            hold=bool(verdict.held_back),
            needs_stop=verdict.stop_required is not None,
            over_risk=over,
            news=verdict.news,
        )

    def clear_guard_latch(self, terminal_id: str, rule_id: str) -> bool:
        """Let go of a breach a guard is holding on this terminal's account, by hand."""
        with self._lock:
            key = self._account_of(terminal_id)
            return key is not None and self._guards.clear_latch(key, rule_id)

    def _account_of(self, terminal_id: str) -> str | None:
        """The account logged into this terminal: the one its latest snapshot names, else the
        one it was last known to be logged into."""
        snap = self._snapshots.get(terminal_id)
        cfg = self._cfg.terminals.get(terminal_id)
        if snap is not None and snap.login is not None:
            return account_key(snap.login, snap.server)
        if cfg is not None:
            return account_key(cfg.last_login, cfg.last_server)
        return None

    def guard_report(self, terminal_id: str) -> GuardReport:
        """How the guards of the account logged into this terminal stand right now.

        The account is the one the latest snapshot names, else the one the terminal was last
        known to be logged into -- so the page can be set up before connecting.
        """
        with self._lock:
            snap = self._snapshots.get(terminal_id)
            cfg = self._cfg.terminals.get(terminal_id)
            key = self._account_of(terminal_id)
            risk = None
            if snap is not None and cfg is not None and cfg.role is Role.SLAVE:
                risk = self._risk_amount(cfg, snap)
            return self._guards.report(key, snap, time.time(), sizing_risk=risk)

    @property
    def failed_total(self) -> int:
        """Order results that came back as failures since this engine started."""
        with self._lock:
            return self._failed_total

    def _set_blocked(self, reason: str | None) -> None:
        """Record, and announce, that detection cannot run -- or that it can again.

        Announced on the *transition* rather than every tick, so it neither floods the log nor
        disappears into it. Detection that has quietly stopped is indistinguishable from a
        master that is simply not trading, which is precisely the failure this project keeps
        designing against: the previous version logged this at DEBUG, below the configured
        level, so a stall was invisible.
        """
        if reason == self._blocked_reason:
            return
        if reason is None:
            log.info("detection resumed; previously paused because %s", self._blocked_reason)
        else:
            log.warning("detection paused: %s -- no actions will be derived", reason)
        self._blocked_reason = reason

    @property
    def blocked_reason(self) -> str | None:
        """Why detection is currently not running, if it is not."""
        with self._lock:
            return self._blocked_reason

    def _set_hold(self, terminal_id: str, reason: str | None) -> None:
        """Record, and announce, that one slave is not being copied to -- or that it is again.

        Deliberately the same shape as :meth:`_set_blocked`: announced on the transition, and
        the recovery announced too. The difference is scope. A block stops detection for
        everyone; a hold stops one slave while the rest go on copying, which is quieter and
        therefore easier to miss, so it gets the same edge-triggered treatment.
        """
        current = self._hold_reason.get(terminal_id)
        if reason == current:
            return
        if reason is None:
            self._hold_reason.pop(terminal_id, None)
            log.info("copying to %s resumed; previously held because %s", terminal_id[:8], current)
        else:
            self._hold_reason[terminal_id] = reason
            log.warning("copying to %s held: %s", terminal_id[:8], reason)

    def _risk_amount(self, cfg: TerminalConfig, snap: Snapshot) -> float | None:
        """This slave's risk budget for one trade, or None if it cannot be known right now.

        A fixed-cash slave needs no balance at all, so an unreadable balance only stops the
        percent-of-balance ones. None means "do not derive anything for this slave", never
        "size it at zero" -- a zero budget is not a soft failure downstream, it is a refusal
        the executor reports as a failed order, which excludes the trade permanently.
        """
        if cfg.sizing_mode is SizingMode.FIXED_CASH_RISK:
            return cfg.risk_cash
        if snap.balance is None:
            return None
        return risk_budget(cfg, snap.balance)

    def _startup_reconcile(self, cfg: TerminalConfig, snap: Snapshot, store: MappingStore) -> None:
        """Match one slave's real positions against the belief store, before anything is derived.

        The store and the terminal can only disagree while we are not running, and they
        disagree in two directions. A record whose position is gone is stale and must be
        dropped, or the first thing the master does is mirrored onto nothing. A position of
        ours with no record is an *orphan*: we opened it and never recorded it, so nobody is
        tracking it and the master closing will not close it.

        Orphans are surfaced and never resolved here. Adopting one silently would break sizing
        honesty, since we cannot know what it was sized against; closing one silently would be
        the copier taking a trading action nobody asked for. A human decides.

        Caller must hold the lock, and must have checked both lists are readable.
        """
        tid = cfg.terminal_id
        positions = snap.open_positions or ()
        orders = snap.pending_orders or ()
        position_tickets = [p.ticket for p in positions]
        order_tickets = [o.ticket for o in orders]

        # A resting order of ours is an orphan on the same grounds as a position: it has not
        # moved money yet, but it will the moment it fills, and nothing would be tracking it.
        orphans = find_orphans(
            store, tid, [(p.ticket, p.symbol, p.volume, p.magic) for p in positions], COPIER_MAGIC
        )
        orphans += find_orphans(
            store, tid, [(o.ticket, o.symbol, o.volume, o.magic) for o in orders], COPIER_MAGIC
        )
        self._orphans[tid] = orphans
        self._orphan_ack.setdefault(tid, set())
        for orphan in orphans:
            log.error(
                "orphan on %s: #%s %s %s lots carries our magic but has no record; "
                "copying to this slave is held until it is acknowledged",
                tid[:8],
                orphan.slave_ticket,
                orphan.symbol,
                orphan.volume,
            )

        stale: list[object] = []
        if self._cfg.dry_run:
            # Simulated beliefs carry a negated master ticket as their slave ticket, so every
            # one of them looks missing against a real position list. Running the stale-row
            # sweep here would delete the whole dry run's state on its first tick.
            if tid not in self._warned_dry_missing:
                self._warned_dry_missing.add(tid)
                log.info(
                    "dry run: not sweeping stale records for %s, its beliefs are simulated",
                    tid[:8],
                )
        else:
            gone = find_missing(store, tid, position_tickets)
            gone_orders = find_missing_orders(store, tid, order_tickets, position_tickets)
            stale = [*gone, *gone_orders]
            for copy in [*gone, *gone_orders]:
                log.warning(
                    "dropping stale record on %s: #%s (%s) is no longer on the terminal; "
                    "it closed while we were not running",
                    tid[:8],
                    copy.slave_ticket,
                    copy.slave_symbol,
                )
                store.forget(tid, copy.master_ticket)

        log.info(
            "startup reconciliation for %s: %d tracked, %d orphans, %d stale",
            cfg.label or tid[:8],
            len(store.for_terminal(tid)),
            len(orphans),
            len(stale),
        )

    def _unacknowledged(self, terminal_id: str) -> list[Orphan]:
        acked = self._orphan_ack.get(terminal_id, set())
        return [o for o in self._orphans.get(terminal_id, []) if o.slave_ticket not in acked]

    def _prepare_slave(self, cfg: TerminalConfig, snap: Snapshot) -> str | None:
        """Why this slave must not be copied to right now, or None if it may be.

        Reconciliation happens here, on the master's tick, rather than when the slave's own
        snapshot arrives. The slave's positions are already in ``_snapshots`` and at most one
        poll old, this is the only place the concrete store and every slave's state are in
        scope together, and it makes the ordering structural: an unreconciled slave is held, so
        stale records are always dropped before a single action is derived against them.

        Caller must hold the lock.
        """
        tid = cfg.terminal_id
        if snap.open_positions is None or snap.pending_orders is None:
            # Both lists or neither, matching the master's guard. Without the orders we cannot
            # tell a filled copy from a cancelled one, which is what makes a record stale.
            return "its own positions cannot be read, so it cannot be reconciled"

        if tid not in self._reconciled:
            self._startup_reconcile(cfg, snap, self._ensure_store())
            self._reconciled.add(tid)

        outstanding = self._unacknowledged(tid)
        if outstanding:
            tickets = ", ".join(f"#{o.slave_ticket}" for o in outstanding)
            return f"{len(outstanding)} untracked position(s) of ours: {tickets}"

        if self._risk_amount(cfg, snap) is None:
            return "its balance cannot be read, so a risk budget cannot be sized"

        return None

    def _slave_links(self) -> list[SlaveLink]:
        """Each enabled slave, paired with whether it may be copied to right now.

        Four conditions, all of which have cost money in copiers that skipped one: the worker
        reports CONNECTED, those reports are *fresh*, the slave has been reconciled against
        the store with no unacknowledged orphans, and it can be sized. Stale-but-CONNECTED is
        the trap in the second one: the last snapshot says the link was fine, and if that were
        trusted a wedged worker would look copyable forever.

        A slave that fails any of them is passed through as ``connected=False`` rather than
        omitted. That is what makes a hold safe: the ledger derives nothing for it, its
        continuity clock stops, and so lifting the hold forces a re-baseline -- while we were
        held we were not copying, so we must not adopt whatever the master opened meanwhile.
        Omitting it instead would let ``_prune`` discard its exclusions, losing decisions that
        are supposed to be final.

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
            if connected and snap is not None:
                hold = self._prepare_slave(cfg, snap)
                self._set_hold(cfg.terminal_id, hold)
                if hold is not None:
                    connected = False
            links.append(
                SlaveLink(cfg=cfg, connected=connected, gate=self._gate(cfg.terminal_id, snap))
            )
        return links

    def orphans(self, terminal_id: str | None = None) -> list[Orphan]:
        """Untracked positions of ours found by startup reconciliation.

        Acknowledged ones stay in this list. Erasing the only record of a real, still-open,
        untracked position the moment the user clicks is the silent loss this whole mechanism
        exists to prevent; the UI dims them instead.
        """
        with self._lock:
            if terminal_id is not None:
                return list(self._orphans.get(terminal_id, []))
            return [o for items in self._orphans.values() for o in items]

    def holds(self) -> dict[str, str]:
        """Held slaves, mapped to why. Empty when every slave is free to copy."""
        with self._lock:
            return dict(self._hold_reason)

    def acknowledge_orphans(self, terminal_id: str) -> int:
        """Sign off this slave's orphans and let it copy again. Returns how many were signed off.

        Acknowledging says "I have seen these positions and taken care of them", not "close
        them" -- nothing here touches a position. The slave re-baselines on its next tick, so
        whatever the master opened during the hold becomes permanently uncopyable for it.
        """
        with self._lock:
            outstanding = self._unacknowledged(terminal_id)
            if not outstanding:
                return 0
            acked = self._orphan_ack.setdefault(terminal_id, set())
            for orphan in outstanding:
                acked.add(orphan.slave_ticket)
            log.warning(
                "acknowledged %d orphan(s) on %s; they remain open and untracked",
                len(outstanding),
                terminal_id[:8],
            )
            return len(outstanding)

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

    def open_copies(self, terminal_id: str) -> list[OpenCopy] | None:
        """The positions and resting orders we believe one slave holds, or None if that cannot
        be read. ``None`` means "could not tell", and callers must treat it as *not empty*."""
        with self._lock:
            return self._open_copies(terminal_id)

    def _open_copies(self, terminal_id: str) -> list[OpenCopy] | None:
        """Read from the store rather than the ledger's memory, and that is the whole point.

        The store is opened lazily -- on the first reconcile, which needs detection on and a
        connected master -- so until then the ledger is an empty in-memory one, and reading it
        would say "nothing" about a slave whose ``mapping.db`` rows are very much alive. Asking
        for the store here opens it a little early, which is harmless: it is the same store the
        first reconcile would open, and it replaces the ledger with one that reads it.

        Caller must hold the lock.
        """
        try:
            self._ensure_store()
            return self._ledger.open_copies(terminal_id)
        except Exception:
            log.exception("could not read the copies open on %s", terminal_id[:8])
            return None

    def copies_at_stake(
        self, terminal_id: str, new: TerminalConfig | None = None
    ) -> dict[str, list[OpenCopy]] | None:
        """The open copies a change to this terminal would give up, by slave, or None if any of
        them cannot be read. ``new`` is a settings edit; None means unassigning it.

        Slaves with nothing open are left out, so an empty dict means there is nothing to ask.
        """
        with self._lock:
            return self._stake(terminal_id, new)

    def _stake_ids(self, terminal_id: str, new: TerminalConfig | None) -> list[str]:
        """Which slaves' copies a change to this terminal gives up.

        A slave (or anything not the master) only ever stakes its own. The master holds no
        copies, but every slave's copies hang off it: unassigning it sweeps the slaves along
        (see :func:`role_actions.unassign`), and disabling it stops anything being mirrored. Any
        other edit to the master leaves the copies managed, so stakes nothing.
        """
        cfg = self._cfg.terminals[terminal_id]
        if cfg.role is not Role.MASTER:
            if new is not None and _only_breakeven_changed(cfg, new):
                # How a stop is managed from here on, never a copy's direction or size: the
                # copies stay, and the new settings apply to them too.
                return []
            return [terminal_id]
        losing = new is None or (cfg.enabled and not new.enabled)
        return [t.terminal_id for t in self._cfg.slaves] if losing else []

    def _stake(
        self, terminal_id: str, new: TerminalConfig | None
    ) -> dict[str, list[OpenCopy]] | None:
        """Caller must hold the lock."""
        stake: dict[str, list[OpenCopy]] = {}
        for tid in self._stake_ids(terminal_id, new):
            held = self._open_copies(tid)
            if held is None:
                return None
            if held:
                stake[tid] = held
        return stake

    def _give_up_agreed(
        self,
        stake: dict[str, list[OpenCopy]] | None,
        orphan: Mapping[str, Collection[int]] | None,
    ) -> str | None:
        """Give up what is at stake, if it is exactly what the person agreed to. None means done
        (or nothing was at stake); otherwise why not, with nothing given up.

        ``orphan`` is, per slave, the master tickets of the copies the person was shown. It has
        to match what is held *now*: a copy that opened while they were deciding is one they
        were not told about, and giving it up unasked is not what they confirmed.

        Caller must hold the lock, and must make its change before releasing it.
        """
        if stake is None:
            return "its open copies could not be read, so they cannot be given up safely"
        if not stake:
            return None
        if orphan is None:
            total = sum(len(held) for held in stake.values())
            return f"{copies_word(total)} would be orphaned; that has to be confirmed"
        agreed = {tid: set(tickets) for tid, tickets in orphan.items() if tickets}
        if agreed != {tid: {c.master_ticket for c in held} for tid, held in stake.items()}:
            return "its open copies changed while you were deciding; try again"
        if any(self._ledger.inflight_on(tid) for tid in stake):
            return "commands to this slave are still in flight; try again in a moment"
        for tid in stake:
            try:
                self._give_up_copies(tid)
            except Exception:
                log.exception("giving up the open copies on %s failed", tid[:8])
                return "giving up its open copies failed part-way; nothing was applied (see log)"
        return None

    def apply_settings(
        self, new: TerminalConfig, *, orphan: Mapping[str, Collection[int]] | None = None
    ) -> str | None:
        """Swap in an edited terminal, or return why it was refused. None means it went in.

        Every edit is one atomic swap, so the drain thread never sees half of one.

        **A change that would stop copies being managed is only made by giving them up.** For a
        slave that is any edit except one to the breakeven settings alone, which only changes how
        a stop is managed from here on: the copier stops managing its copies -- nothing closes
        them when the master does, moves their stops, or mirrors a partial close -- and they
        stay open on the slave. That replaces a rule per setting -- which ones were safe to
        change under an open copy, which had to be refused -- with one rule that does not depend
        on knowing the answer. For the master it is disabling it, which strands every slave's
        copies the same way. See :meth:`_give_up_agreed` for what ``orphan`` must hold.

        The check, the giving up and the swap happen under one hold of the lock, and the drain
        thread dispatches under the same lock. Doing them in turn would leave a gap for an open
        to slip into. The UI asks earlier too, to have something to show; this is the check
        that cannot be raced.
        """
        with self._lock:
            tid = new.terminal_id
            old = self._cfg.terminals[tid]
            if new == old:
                return None
            if new.role is not old.role:
                # replace_terminal refuses this too, but only after the copies had been given
                # up, and a caller bug must not cost a person their tracking for nothing.
                raise ValueError("a settings edit cannot change a terminal's role; use set_role")
            refusal = self._give_up_agreed(self._stake(tid, new), orphan)
            if refusal is not None:
                return refusal
            self._cfg.replace_terminal(new)
        return None

    def set_paused(self, terminal_id: str, paused: bool) -> None:
        """Pause or resume one slave. Unlike :meth:`apply_settings` this gives nothing up:
        a pause declines new trades only, so the copies the slave holds stay managed.

        Under the lock, so a tick never derives a slave's actions from one side of the switch
        and dispatches them from the other. The caller still has to save the config.
        """
        with self._lock:
            cfg = self._cfg.terminals[terminal_id]
            if cfg.role is not Role.SLAVE:
                raise ValueError("only a slave can be paused")
            if cfg.paused is not paused:
                self._cfg.replace_terminal(cfg.model_copy(update={"paused": paused}))

    def unassign(
        self, terminal_id: str, *, orphan: Mapping[str, Collection[int]] | None = None
    ) -> str | None:
        """Take this terminal's role away, giving up the copies that strands. None means done.

        The same rule as :meth:`apply_settings`, for the same reason. Unassigning a slave stops
        its copies being managed; unassigning the master also unassigns every slave (see
        :func:`role_actions.unassign`), so all of theirs go. Before this asked, those copy
        records stayed behind -- and under the *next* master, whose tickets they do not match,
        the first tick proposed closing every one of them.

        The caller still has to save the config and :meth:`sync`, which stops the workers.
        """
        with self._lock:
            refusal = self._give_up_agreed(self._stake(terminal_id, None), orphan)
            if refusal is not None:
                return refusal
            role_actions.unassign(self._cfg, terminal_id)
        return None

    def setup_stake(self, setup: Setup) -> dict[str, list[OpenCopy]] | None:
        """The open copies loading this setup would give up, by slave, or None if any of them
        cannot be read. An empty dict means there is nothing to ask."""
        with self._lock:
            return self._setup_stake(setup)

    def _setup_stake(self, setup: Setup) -> dict[str, list[OpenCopy]] | None:
        """Caller must hold the lock."""
        stake: dict[str, list[OpenCopy]] = {}
        for tid in setups_mod.losing(setup, self._cfg):
            held = self._open_copies(tid)
            if held is None:
                return None
            if held:
                stake[tid] = held
        return stake

    def load_setup(
        self, setup: Setup, *, orphan: Mapping[str, Collection[int]] | None = None
    ) -> str | None:
        """Put a saved setup's roles in place, and copying on or off as it was saved. None means
        done; otherwise why not, with nothing changed.

        The same rule as :meth:`unassign`, for the same reason: a slave taken off its master --
        or left on while the master changes under it -- has its copies given up, and only if the
        person agreed to exactly those. One that keeps its master keeps its copies.

        The caller still has to save the config and :meth:`sync`.
        """
        with self._lock:
            found = setups_mod.problems(setup, self._cfg)
            if found:
                return "; ".join(found)
            refusal = self._give_up_agreed(self._setup_stake(setup), orphan)
            if refusal is not None:
                return refusal
            setups_mod.apply_roles(setup, self._cfg)
        return None

    def _give_up_copies(self, terminal_id: str) -> None:
        """Give up every copy on one slave, and say so where a person will see it.

        Registered as orphans that are already acknowledged: the person was told, in advance,
        and agreed. That puts them in the same list, and on the same panel, as an orphan found at
        startup -- open, unmanaged, not closed by the master -- without holding the slave, so
        it goes on copying under its new settings. A restart finds them again by the usual
        route, and holds until they are acknowledged again.

        A dry run has nothing real to report: its copies are simulated and its slave tickets are
        not tickets. The ledger still lets them go and excludes them, so the rehearsal does what
        live would.

        Caller must hold the lock and has ruled out anything in flight.
        """
        let_go = self._ledger.abandon(terminal_id)
        label = self._cfg.terminals[terminal_id].label or terminal_id[:8]
        if self._cfg.dry_run:
            log.warning("dry run: %d simulated copies on %s let go", len(let_go), label)
            return
        orphans = self._orphans.setdefault(terminal_id, [])
        acknowledged = self._orphan_ack.setdefault(terminal_id, set())
        for copy in let_go:
            log.warning(
                "orphaned on %s: %s #%s %s %s lots (master #%s); settings changed, so it is no "
                "longer copied, closed or moved, and stays open",
                label,
                "order" if copy.resting else "position",
                copy.slave_ticket,
                copy.symbol,
                copy.volume,
                copy.master_ticket,
            )
            if copy.slave_ticket is not None:
                orphans.append(
                    Orphan(terminal_id, copy.slave_ticket, copy.symbol, copy.volume or 0.0)
                )
                acknowledged.add(copy.slave_ticket)

    def clear_detection_state(self) -> None:
        """Forget every believed copy and the action history.

        Safe in a dry run, where beliefs are simulated. **In live mode this abandons real
        positions** -- they stay open with nothing tracking them -- so it warns, and the caller
        is expected to have asked first.
        """
        with self._lock:
            store = self._ensure_store()
            if not self._cfg.dry_run:
                log.warning(
                    "clearing %d LIVE copy records; those positions stay open and will appear "
                    "as orphans on the next startup reconciliation",
                    len(self._ledger.believed()),
                )
            store.clear()
            self._ledger.reset_session_state()
            self._actions.clear()
            self._day.clear(ts=time.time())
            # Every position that was ours is now unrecorded, which is the definition of an
            # orphan. Re-reconcile rather than carry forward a clean bill of health.
            for tid in list(self._cfg.terminals):
                self._forget_reconciliation(tid)

    def _ensure_store(self) -> MappingStore:
        """The belief store for the current mode, opening or swapping it as needed.

        Swapping on a mode change is the safeguard that keeps a dry run honest: simulated
        beliefs live in their own file, so turning copying live cannot inherit a position that
        was only ever pretended. Session state is reset with it, since exclusions and
        continuity were reasoned about against the other set of beliefs.

        Caller must hold the lock.
        """
        dry = self._cfg.dry_run
        if self._store is not None and self._store_is_dry == dry:
            return self._store

        if self._store is not None:
            self._store.close()
            outstanding = self._ledger.inflight_count
            if outstanding:
                log.warning(
                    "changing dry_run with %d command(s) in flight; their results can no "
                    "longer be matched and will be dropped",
                    outstanding,
                )
        path = self._dryrun_path if dry else self._mapping_path
        self._store = MappingStore(path)
        self._ledger = CopyLedger(self._store)
        self._store_is_dry = dry
        # The rows in the new store are a different set of claims about the world, so every
        # slave has to be matched against them again before it may be copied to.
        for tid in list(self._reconciled | set(self._orphans) | set(self._hold_reason)):
            self._forget_reconciliation(tid)
        log.info(
            "belief store: %s (%s), %d copies known",
            path,
            "dry run" if dry else "LIVE",
            len(self._ledger.believed()),
        )
        return self._store

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
