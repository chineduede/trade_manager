"""Supervisor: owns one worker process per designated terminal and aggregates their state.

Deployment shape for Stage 1: the engine runs inside the UI process on a background thread,
and workers are subprocesses. ``zele-mt5`` put a WebSocket IPC server between its engine and
any consumer, which is the right call once there are several consumers (it grew a web dashboard
that used exactly that seam; this project tried one and dropped it) -- but there is precisely one consumer, the desktop window, so the
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
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import role_actions
from . import setups as setups_mod
from .breakeven import (
    breakeven_level,
    buffer_distance,
    check_breakeven_price,
    guard_stop,
    master_breakeven_due,
    stop_past_entry,
)
from .catalog import CLASS_WORDS, Catalog, CatalogStore
from .claims import ClaimBook, ClaimRefusedError, claim_keys
from .config import (
    VIRTUAL_MASTER_ID,
    AppConfig,
    AppMode,
    BreakevenMode,
    Role,
    SizingMode,
    TerminalClosing,
    TerminalConfig,
    account_key,
    merge_discovery,
)
from .config import (
    save as save_config,
)
from .copy_stats import StatsBook, TerminalStats
from .ctrader.worker import run_ctrader_worker
from .day_book import DayBook, MasterEvent, Outcome, SlaveOutcome
from .discovery import discover_terminals
from .guards import NO_VERDICT, GuardBook, GuardReport, GuardsDoc, Verdict
from .guards.book import CloseAct, NewsAct
from .history import ORPHANED, GuardAction, HistoryStore, Step, Trade, step_for
from .logging_setup import get_logger
from .news import NewsCalendar
from .paths import (
    dryrun_store_path,
    history_db_path,
    log_dir,
    mapping_db_path,
    virtual_book_path,
)
from .prop_accounts import PropStore
from .protocol import (
    ACCOUNT_TRADE_MODE_DEMO,
    COPIER_MAGIC,
    DISPATCHABLE_KINDS,
    ActionKind,
    Command,
    CommandKind,
    GuardNote,
    OrderCommand,
    OrderResult,
    OrderType,
    PendingOrder,
    Position,
    PositionLive,
    PositionType,
    Quote,
    RatesRequest,
    RatesResult,
    Snapshot,
    TradeConstraint,
    WatchCommand,
    WorkerState,
)
from .reconcile import (
    Action,
    CopiedPosition,
    CopyLedger,
    GuardGate,
    OpenCopy,
    SkipReason,
    SlaveLink,
    news_hits,
    protective_sl,
)
from .settings_edit import copies_word
from .setups import Setup
from .sizing import risk_budget
from .store import (
    MappingStore,
    MasterTrade,
    Orphan,
    find_missing,
    find_missing_orders,
    find_orphans,
)
from .symbols import undecorate
from .virtual import VirtualBook
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
    claim_refused: str | None = None
    """Why no worker was spawned: another running profile holds this terminal or its
    account. The engine retries every few seconds, so this clears by itself once the
    other instance lets go -- its locks die with its process, close included."""

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
            if self.claim_refused is not None:
                return self.claim_refused
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


_MASTER_BE_RETRY_S = 15.0
"""After the master's breakeven was not set -- the price fell back inside it, a busy server --
how long before it is asked again. A refusal that is not about the moment waits far longer."""
_MASTER_BE_GIVE_UP_S = 300.0

_BE_RETRY_FACTOR = 1.5
"""After a refused breakeven, how much more room than the refused attempt the price needs
before the next try. Each try is at a strictly better distance, so a broker that keeps
refusing is asked a handful of times as the trade runs, never once per tick."""


def _undecorated(
    positions: Sequence[Position], orders: Sequence[PendingOrder], master: TerminalConfig
) -> tuple[Sequence[Position], Sequence[PendingOrder]]:
    """The master's lists with its broker's prefix/suffix taken off every symbol.

    Done once, here, before anything else reads a symbol, so that the slaves' filters, renames,
    decorations and resolution, the news guard and the history all see one name -- the plain
    one -- instead of each having to know about the master's broker. Only the copier's view
    changes: the master's own page still shows what its terminal calls the instrument.
    """
    prefix, suffix = master.symbol_prefix, master.symbol_suffix
    if not prefix and not suffix:
        return positions, orders
    return (
        [replace(p, symbol=undecorate(p.symbol, prefix, suffix)) for p in positions],
        [replace(o, symbol=undecorate(o.symbol, prefix, suffix)) for o in orders],
    )


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
        claims: ClaimBook | None = None,
        virtual_path: Path | None = None,
    ) -> None:
        # Windows only offers the spawn start method; request it explicitly so behaviour is
        # identical if this ever runs on Linux for testing.
        self._ctx = mp.get_context("spawn")
        self._cfg = cfg
        self._out_q: mp.Queue = self._ctx.Queue(maxsize=10_000)
        self._workers: dict[str, _Worker] = {}
        self._claims = claims
        """Cross-profile claims on terminals and accounts. ``None`` -- the tests' engines, and
        anything else that is not a real app instance -- claims nothing and refuses nothing."""
        self._claimed: dict[str, tuple[str, ...]] = {}
        """Per terminal: the claim keys its worker holds, released when the worker is reaped."""
        self._claim_refused: dict[str, str] = {}
        """Per terminal: why its worker was not spawned, for the UI. Retried on every sync,
        and every few seconds by the drain loop (see :meth:`_retry_refused_claims`)."""
        self._claims_retry_at = 0.0
        """Monotonic time before which :meth:`_retry_refused_claims` stays quiet."""
        self._snapshots: dict[str, Snapshot] = {}
        self._received_at: dict[str, float] = {}
        self._symbol_lists: dict[str, tuple[str, ...]] = {}
        """Each terminal's broker symbol catalog, as its worker last pushed it (once per
        connection; later snapshots carry None, so the cache outlives them). UI search only."""
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
        self._seen_account: dict[str, str] = {}
        """Per terminal: the account key its *current worker* last reported. A switch is a
        change between a worker's own snapshots, never a disagreement with the config -- a
        rescan can regress the config off a stale ``common.ini``, and that must read as
        bookkeeping to quietly correct, not as an account switch to react to."""

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
        self._master_be_wait: dict[int, float] = {}
        """Per master trade: not before this monotonic time is its breakeven asked for again,
        after an attempt that did not set it."""
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
        self._catalog = CatalogStore()
        """The shared symbol catalog, for the rules that read asset classes."""
        self._props = PropStore()
        """The prop account containers: a linked account reads its rules under its
        container's identity."""
        self._guards = GuardBook(
            guards if guards is not None else GuardsDoc(),
            guard_state_path,
            self._calendar,
            self._catalog,
            self._props,
        )
        """Each account's prop-firm rules and how they stand."""
        self._verdicts: dict[str, Verdict] = {}
        self._news_done: dict[str, set[int]] = {}
        """Per slave, the master tickets a news rule has acted on in the window now open: each
        copy is acted on once per window, which ends when the slave's rules stop acting."""
        self._guard_be: set[tuple[str, int]] = set()
        """Copies a guard moved to breakeven: if the broker refuses that stop, they are closed."""
        self._guard_pending: dict[tuple[str, int, ActionKind], GuardNote] = {}
        """What a guard asked of a copy, waiting for the diff to derive the action it goes on."""
        self._guard_closed: dict[str, set[int]] = {}
        """Per slave, the slave tickets a close-the-worst rule has closed that its snapshot
        still shows open. While one lingers, no further copy is picked: the rule is measured
        again only on what is really left, so a laggy snapshot never closes one copy too many."""
        """Per slave: what its guards said on its latest snapshot. Read when the master's tick
        derives new trades for it, and when a trade is sent."""

        self._dryrun_path = state_path if state_path is not None else dryrun_store_path()
        self._mapping_path = mapping_path if mapping_path is not None else mapping_db_path()
        self._store: MappingStore | None = None
        self._store_is_dry: bool | None = None
        """Which mode the open store belongs to, so a change of ``dry_run`` swaps it rather than
        carrying simulated beliefs into a live session."""
        self._ledger = CopyLedger()

        # --- direct trading (the virtual master) ---------------------------------------------
        self._virtual_path = virtual_path
        self._virtual: VirtualBook | None = None
        """The synthetic master book, open only while the profile is in trading mode. Its tick
        thread runs either way and no-ops while this is None, so a mode switch needs no thread
        juggling."""
        if cfg.mode is AppMode.TRADING:
            self._virtual = VirtualBook(
                virtual_path if virtual_path is not None else virtual_book_path()
            )
        self._virtual_seq = 0
        self._virtual_thread: threading.Thread | None = None
        self._stop_virtual = threading.Event()
        self._rescan_thread: threading.Thread | None = None
        self._stop_rescan = threading.Event()
        self._feed_tid: str | None = None
        """The terminal whose quotes feed the book, as last picked: the pinned one when it is
        connected, else any connected MT5 slave. Remembered only to announce changes."""
        self._watch_extra: str | None = None
        """One symbol the order ticket is previewing beyond what the book holds, so its quote
        is live before anything is placed."""
        self._watch_sent: tuple[str, WatchCommand] | None = None
        """The watch last sent and to whom, so an unchanged watch costs no queue traffic."""
        self._rates: RatesResult | None = None
        """The minichart's bars, as the feed last answered them. Latest wins."""
        self._rates_asked: tuple[str, int, str] | None = None
        """What was last asked for and of whom: (symbol, timeframe, feed terminal)."""
        self._rates_asked_at = 0.0

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        """Start the drain thread and bring workers in line with config."""
        if self._drain_thread is None:
            self._stop_drain.clear()
            self._drain_thread = threading.Thread(
                target=self._drain_loop, name="engine-drain", daemon=True
            )
            self._drain_thread.start()
        if self._virtual_thread is None:
            self._stop_virtual.clear()
            self._virtual_thread = threading.Thread(
                target=self._virtual_loop, name="engine-virtual", daemon=True
            )
            self._virtual_thread.start()
        if self._rescan_thread is None:
            self._stop_rescan.clear()
            self._rescan_thread = threading.Thread(
                target=self._rescan_loop, name="engine-rescan", daemon=True
            )
            self._rescan_thread.start()
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
        self._stop_virtual.set()
        self._stop_rescan.set()
        self._calendar.stop()
        thread = self._drain_thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._drain_thread = None
        vthread = self._virtual_thread
        if vthread is not None:
            vthread.join(timeout=2.0)
            self._virtual_thread = None
        rthread = self._rescan_thread
        if rthread is not None:
            rthread.join(timeout=2.0)
            self._rescan_thread = None
        self._history.close()
        if self._claims is not None:
            self._claims.release_all()

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
                if not tcfg.is_ctrader and (
                    tcfg.exe_path is None or not Path(tcfg.exe_path).is_file()
                ):
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

            # A terminal no longer wanted keeps no refusal note: the person unassigned it,
            # so "in use elsewhere" would be stale the moment it is shown.
            for tid in list(self._claim_refused):
                if tid not in wanted:
                    del self._claim_refused[tid]

            for tid, tcfg in wanted.items():
                existing = self._workers.get(tid)
                if existing is not None and existing.process.is_alive():
                    continue
                if existing is not None:
                    log.warning("worker for %s died; respawning", tid[:8])
                    self._reap(tid)
                self._spawn_worker(tcfg)

    def _retry_refused_claims(self) -> None:
        """Re-attempt claims another profile refused us, on a slow tick.

        The locks behind a claim die with the process that holds them, so the moment the
        other window closes -- X button, crash, anything -- the terminal is free. But
        nothing used to try again until the person forced a sync (a rescan, a role
        change), and the card said "in use" long after it wasn't. The drain loop calls
        this every pass; it costs nothing unless a refusal is on record and a few
        seconds have gone by since the last try.
        """
        if not self._claim_refused or time.monotonic() < self._claims_retry_at:
            return
        with self._lock:
            self._claims_retry_at = time.monotonic() + 3.0
            for tid in list(self._claim_refused):
                tcfg = self._cfg.terminals.get(tid)
                if tcfg is None or tcfg.role is Role.UNASSIGNED or not tcfg.enabled:
                    # Same rule as sync(): a terminal no longer wanted keeps no stale note.
                    del self._claim_refused[tid]
                    continue
                if tid in self._workers:
                    self._claim_refused.pop(tid, None)
                    continue
                if not tcfg.is_ctrader and (
                    tcfg.exe_path is None or not Path(tcfg.exe_path).is_file()
                ):
                    continue
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
                if tcfg.is_ctrader or (tcfg.exe_path is not None and Path(tcfg.exe_path).is_file()):
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
        if self._claims is not None:
            keys = claim_keys(tcfg)
            try:
                self._claims.acquire(keys)
            except ClaimRefusedError as exc:
                if self._claim_refused.get(tcfg.terminal_id) != str(exc):
                    log.warning("not connecting %s: %s", tcfg.label, exc)
                self._claim_refused[tcfg.terminal_id] = str(exc)
                return
            self._claimed[tcfg.terminal_id] = tuple(keys)
        self._claim_refused.pop(tcfg.terminal_id, None)
        cmd_q: mp.Queue = self._ctx.Queue(maxsize=64)
        if tcfg.ctrader_account_id is not None:
            log_file = log_dir() / f"worker-ctrader-{tcfg.ctrader_account_id}.log"
            process = self._ctx.Process(
                target=run_ctrader_worker,
                name=f"ctrader-{tcfg.ctrader_account_id}",
                args=(
                    tcfg.terminal_id,
                    tcfg.ctrader_account_id,
                    tcfg.ctrader_live,
                    tcfg.last_server or "",
                    self._out_q,
                    cmd_q,
                    str(log_file),
                    self._cfg.poll_interval_ms,
                    list(self._cfg.reconnect_backoff_s),
                ),
                daemon=True,
            )
        else:
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
        if self._claims is not None:
            # Only once the process is gone: releasing while it still runs would let another
            # profile attach a terminal this one is mid-shutdown on.
            self._claims.release(list(self._claimed.pop(terminal_id, ())))
        self._snapshots.pop(terminal_id, None)
        self._received_at.pop(terminal_id, None)
        # The next worker's first report is learning, not a switch: whatever happened in the
        # terminal while no worker watched it was not watched, the same rule join_grace
        # applies to trades (startup reconciliation owns that gap).
        self._seen_account.pop(terminal_id, None)
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
            self._retry_refused_claims()
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
            if isinstance(snap, RatesResult):
                with self._lock:
                    self._rates = snap
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
                if snap.symbols is not None:
                    self._symbol_lists[snap.terminal_id] = snap.symbols
                self._stats.on_state(snap.terminal_id, snap.state, now=time.monotonic())
                if not self._record_account(snap):
                    # A slave switched under its role was just unassigned and its worker
                    # stopped: nothing further is filed for it this tick.
                    continue
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

    def _record_account(self, snap: Snapshot) -> bool:
        """Keep ``last_login``/``last_server`` tracking the account the worker actually sees,
        and react when what it sees is a *switch* made under a role.

        A person can log a connected terminal into another account from MT5 itself, under a
        running worker, and the snapshot is the first and only place that shows. Left
        unrecorded, everything keyed by the *config's* account -- the given name on the card,
        what a rename names, the guards and claims picked at the next spawn -- stays filed
        under the account the terminal left. Discovery corrects the config too, but only on a
        rescan and only off ``common.ini``; a live worker should not need either. Saved at
        once: rare, and a crash before an incidental save would resurrect the stale account.

        A *switch* is a change between the current worker's own reports (``_seen_account``),
        never a disagreement with the config: the first account a worker reports is learning,
        and a config corrected elsewhere -- a rescan off a stale ``common.ini`` -- is
        bookkeeping to quietly re-record, not an event. A real switch under a role is
        something the app never allows from inside it, and :meth:`_on_switched_under_a_role`
        makes the state honest; it also takes the live-trading override with it
        (:meth:`_withdraw_live_override`).

        Returns False when the terminal was unassigned here and its worker stopped, so the
        drain loop files nothing further from this snapshot. Caller must hold the lock.
        cTrader snapshots echo the config's own server back, so for them this never fires.
        """
        if snap.login is None:
            return True
        tcfg = self._cfg.terminals.get(snap.terminal_id)
        if tcfg is None:
            return True
        # A worker that did not read the server keeps the one on record rather than erasing it.
        server = snap.server or tcfg.last_server
        now_key = account_key(snap.login, server)
        assert now_key is not None  # login was checked above
        seen = self._seen_account.get(snap.terminal_id)
        self._seen_account[snap.terminal_id] = now_key
        switched = seen is not None and seen != now_key
        if (tcfg.last_login, tcfg.last_server) == (snap.login, server):
            # The config already agrees; all that could remain is the switch reaction (the
            # config may have been corrected before the worker's own report moved).
            if switched:
                keep = not self._on_switched_under_a_role(
                    snap.terminal_id, tcfg.role, seen, now_key
                )
                self._withdraw_live_override(snap.terminal_id, seen, now_key)
                self._save_recorded()
                return keep
            return True
        was = account_key(tcfg.last_login, tcfg.last_server)
        self._cfg.replace_terminal(
            tcfg.model_copy(update={"last_login": snap.login, "last_server": server})
        )
        log.info(
            "%s is logged in to %s, not %s as the config said; recorded",
            self._cfg.display_name(snap.terminal_id),
            now_key,
            was or "any account yet",
        )
        keep = True
        if switched:
            keep = not self._on_switched_under_a_role(snap.terminal_id, tcfg.role, seen, now_key)
            self._withdraw_live_override(snap.terminal_id, seen, now_key)
        self._save_recorded()
        return keep

    def _withdraw_live_override(self, tid: str, was: str | None, now_key: str | None) -> None:
        """Take ``allow_live_trading`` back off after an account switch: it was typed and
        confirmed for the account the terminal was on, and the account it moved to never got
        that confirmation. Off, the demo-only guard holds a real account back until it is
        deliberately allowed again. Caller must hold the lock and saves afterwards."""
        tcfg = self._cfg.terminals.get(tid)
        if tcfg is None or not tcfg.allow_live_trading:
            return
        self._cfg.replace_terminal(tcfg.model_copy(update={"allow_live_trading": False}))
        log.warning(
            "%s: live trading is no longer allowed; it was allowed while it was on %s, "
            "and %s was never confirmed",
            self._cfg.display_name(tid),
            was,
            now_key,
        )

    def _save_recorded(self) -> None:
        try:
            save_config(self._cfg)
        except OSError:
            log.exception("could not save the recorded account; it holds for this run")

    def _on_switched_under_a_role(
        self, tid: str, role: Role, was: str | None, now_key: str | None
    ) -> bool:
        """React to an account switch made in the terminal itself. Returns True when the
        terminal was unassigned and its worker stopped.

        The app's own rule is that only an unassigned terminal is ever switched (see
        :mod:`mt5copier.account_switch` for why): a slave's copy records name tickets that
        live on the account it left, so the first master close would fire commands at tickets
        the new account has never heard of, while the real positions sat on the old account
        with nothing managing them -- and every new master trade would be copied onto an
        account nobody set up as a slave. The switch already happened, so there is nothing
        left to refuse; instead the state is made what the rule would have required. The
        copies are given up exactly as an unassign gives them up (open on the old account,
        excluded from re-copying; a command already in flight finds no row and is logged,
        which is the lesser harm), the role is removed, and the worker is stopped -- but the
        terminal window is left alone: the person is working in it.

        A switched master is worse: its old positions leaving the snapshot would read as
        "the master went flat", closing every copy on every slave, and the new account's
        positions would read as fresh trades to copy. Copying stops instead -- recorded here,
        before this same snapshot reaches :meth:`_reconcile`, which checks
        ``copying_enabled`` first, so that tick can never run. Nothing is closed or opened;
        what the new account means is the person's decision. Caller must hold the lock.
        """
        name = self._cfg.display_name(tid)
        if role is Role.SLAVE:
            self._give_up_copies(tid)
            role_actions.unassign(self._cfg, tid)
            self._stop_worker(tid)
            log.error(
                "%s was logged in to %s from the terminal itself; it was a slave on %s, and "
                "only an unassigned terminal is switched, so it is unassigned: its copies "
                "stay open on %s unmanaged, and nothing is copied to %s unless it is "
                "assigned again",
                name,
                now_key,
                was,
                was,
                now_key,
            )
            return True
        if role is Role.MASTER:
            self._cfg.copying_enabled = False
            log.error(
                "the master %s was logged in to %s from the terminal itself; copying was set "
                "up from %s, so it is stopped before anything is closed or copied -- review "
                "the roles, then turn copying back on",
                name,
                now_key,
                was,
            )
        return False

    def _rescan_loop(self) -> None:
        """Re-discover terminals from disk on the configured cadence
        (``rescan_interval_s``), so an unassigned terminal's card follows a login made in
        MT5 itself without anyone pressing rescan.

        Passive, exactly as the rescan button is: directories and ``common.ini`` are read,
        nothing is launched and nothing connects. A *connected* terminal's worker is the
        fresher truth -- MT5 may not have flushed its ``common.ini`` yet -- so after the
        merge, whatever a live snapshot reports wins back; a stale file can therefore never
        regress a connected terminal's account (and could not trigger the switch reaction
        anyway, which reads only the worker's own reports). Saved only when the sweep
        changed something, so the config file is not rewritten every cycle.
        """
        while not self._stop_rescan.wait(max(self._cfg.rescan_interval_s, 10.0)):
            try:
                self._rescan_once()
            except Exception:
                log.exception("periodic discovery failed; trying again on schedule")

    def _rescan_once(self) -> None:
        """One sweep of :meth:`_rescan_loop`, factored out so it can be driven directly."""
        found = discover_terminals()
        with self._lock:
            before = self._cfg.model_dump(mode="json")
            merge_discovery(self._cfg, found)
            for tid, snap in self._snapshots.items():
                tcfg = self._cfg.terminals.get(tid)
                if tcfg is None or snap.login is None:
                    continue
                server = snap.server or tcfg.last_server
                if (tcfg.last_login, tcfg.last_server) != (snap.login, server):
                    self._cfg.replace_terminal(
                        tcfg.model_copy(update={"last_login": snap.login, "last_server": server})
                    )
            if self._cfg.model_dump(mode="json") != before:
                self._save_recorded()

    # --- direct trading: the virtual master ---------------------------------------------------

    def _virtual_loop(self) -> None:
        """Tick the virtual master on the worker cadence, while trading mode is on.

        Runs whether or not it is -- it no-ops with the book closed -- so a mode switch never
        has to start or stop a thread. A real master's reconcile is clocked by its snapshots
        arriving; the virtual master has no worker, so this thread is its clock.
        """
        period = max(self._cfg.poll_interval_ms, 50) / 1000.0
        while not self._stop_virtual.wait(period):
            try:
                self.virtual_tick()
            except Exception:
                # Same containment as the drain loop: a crash here must not silently stop
                # the book ticking -- or worse, kill the thread and freeze direct mode.
                log.exception("virtual master tick failed; skipping this tick")

    def virtual_tick(self, now: float | None = None) -> None:
        """One turn of the virtual master: prices act on the book, then the book is
        reconciled onto the slaves exactly as a real master's snapshot would be."""
        with self._lock:
            book = self._virtual
            if book is None:
                return
            when = time.time() if now is None else now
            feed_tid, quotes = self._feed_quotes()
            self._sync_watch(feed_tid)
            book.tick(quotes, when)
            self._virtual_seq += 1
            positions = book.positions()
            snap = Snapshot(
                terminal_id=VIRTUAL_MASTER_ID,
                state=WorkerState.CONNECTED,
                seq=self._virtual_seq,
                ts=when,
                positions=len(positions),
                open_positions=positions,
                live_positions=self._virtual_live(positions, quotes),
                pending_orders=book.orders(),
            )
            self._snapshots[VIRTUAL_MASTER_ID] = snap
            self._received_at[VIRTUAL_MASTER_ID] = time.monotonic()
            self._reconcile(snap)

    @staticmethod
    def _virtual_live(
        positions: Sequence[Position], quotes: Mapping[str, tuple[float, float]]
    ) -> tuple[PositionLive, ...]:
        """The book's per-tick half, read off the feed: each position priced at the side its
        close would execute on. None where the feed has no quote, like any failed read."""
        out: list[PositionLive] = []
        for p in positions:
            quote = quotes.get(p.symbol)
            price = None
            if quote is not None:
                price = quote[0] if p.type is PositionType.BUY else quote[1]
            out.append(PositionLive(ticket=p.ticket, price_current=price))
        return tuple(out)

    def _feed_ok(self, terminal_id: str) -> bool:
        """Whether this terminal can feed quotes right now: an enabled MT5 slave whose worker
        is connected and fresh. cTrader accounts are not offered yet -- quotes come off MT5's
        ``symbol_info_tick``, and the trendbar path is its own piece of work."""
        cfg = self._cfg.terminals.get(terminal_id)
        if cfg is None or cfg.is_ctrader or cfg.role is not Role.SLAVE or not cfg.enabled:
            return False
        snap = self._snapshots.get(terminal_id)
        received = self._received_at.get(terminal_id)
        fresh = (
            received is not None and (time.monotonic() - received) <= self._cfg.heartbeat_timeout_s
        )
        return fresh and snap is not None and snap.state is WorkerState.CONNECTED

    def _pick_feed(self) -> str | None:
        """The terminal to quote from: the pinned one while it is usable, else whoever is
        feeding now while *it* is usable, else the first connected MT5 slave by label, else
        none. Sticky on purpose -- the first slave to connect keeps the job, and a slave
        connecting later never takes it over mid-session; only the pin outranks a working
        feed. Changes are announced once, not per tick -- a feed that fails over silently
        would leave 'why did my pending fill late' unanswerable."""
        pinned = self._cfg.feed_terminal_id
        chosen = pinned if pinned is not None and self._feed_ok(pinned) else None
        if chosen is None and self._feed_tid is not None and self._feed_ok(self._feed_tid):
            chosen = self._feed_tid
        if chosen is None:
            ordered = sorted(self._cfg.slaves, key=lambda c: (c.label.lower(), c.terminal_id))
            chosen = next((t.terminal_id for t in ordered if self._feed_ok(t.terminal_id)), None)
        if chosen != self._feed_tid:
            if chosen is None:
                log.warning(
                    "direct trading has no price feed: no connected MT5 slave to quote from; "
                    "nothing fills until one connects"
                )
            else:
                log.info("direct trading quotes come from %s", self._cfg.display_name(chosen))
            self._feed_tid = chosen
        return chosen

    def _feed_decor(self) -> tuple[str, str, dict[str, str]]:
        """The feed broker's configured decoration, for taking its own names back to clean
        ones: prefix, suffix, and its symbol map reversed (broker name -> clean name).
        Caller must hold the lock."""
        tid = self._feed_tid
        cfg = self._cfg.terminals.get(tid) if tid is not None else None
        if cfg is None:
            return "", "", {}
        back = {v.upper(): k for k, v in cfg.symbol_map.items()}
        return cfg.symbol_prefix, cfg.symbol_suffix, back

    def feed_decor(self) -> tuple[str, str, dict[str, str]]:
        """The feed's decoration for callers cleaning its catalog, like the ticket's search."""
        with self._lock:
            return self._feed_decor()

    def _feed_clean(self, symbol: str) -> str:
        """A symbol as the book keys it: upper-cased, the feed broker's decoration taken off.

        The book, the watch and every slave's resolution all start from the clean name; a
        name that enters as the feed lists it -- typed into the ticket, or picked before the
        search cleaned them -- would quote and fill on the feed alone while every other slave
        refuses it as not found. Caller must hold the lock, after the feed is picked."""
        prefix, suffix, back = self._feed_decor()
        name = symbol.strip()
        return (back.get(name.upper()) or undecorate(name, prefix, suffix)).upper()

    def _feed_quotes(self) -> tuple[str | None, dict[str, tuple[float, float]]]:
        """The feed's latest prices by clean symbol name. Caller must hold the lock."""
        tid = self._pick_feed()
        if tid is None:
            return None, {}
        snap = self._snapshots.get(tid)
        quotes = {q.symbol: (q.bid, q.ask) for q in (snap.quotes if snap is not None else ())}
        return tid, quotes

    def _sync_watch(self, feed_tid: str | None) -> None:
        """Keep the feed worker quoting what the book needs: every symbol it holds something
        on, plus the one the ticket is previewing. Sent only when the watch or the feed
        changes, and the old feed is told to stop when it is handed over."""
        book = self._virtual
        if book is None or feed_tid is None:
            return
        cfg = self._cfg.terminals.get(feed_tid)
        if cfg is None:
            return
        needed = book.symbols()
        if self._watch_extra:
            needed = needed | {self._watch_extra}
        cmd = WatchCommand(
            symbols=tuple(sorted(needed)),
            symbol_prefix=cfg.symbol_prefix,
            symbol_suffix=cfg.symbol_suffix,
            symbol_map=tuple(sorted(cfg.symbol_map.items())),
        )
        if self._watch_sent == (feed_tid, cmd):
            return
        if self._watch_sent is not None and self._watch_sent[0] != feed_tid:
            old = self._workers.get(self._watch_sent[0])
            if old is not None:
                try:
                    old.cmd_q.put_nowait(WatchCommand())
                except Exception as exc:
                    log.debug("could not clear the old feed's watch: %r", exc)
        worker = self._workers.get(feed_tid)
        if worker is None:
            return
        try:
            worker.cmd_q.put_nowait(cmd)
        except Exception as exc:
            log.debug("could not send the watch to %s: %r", feed_tid[:8], exc)
            return
        self._watch_sent = (feed_tid, cmd)

    # --- direct trading: the person's own orders ----------------------------------------------

    _NOT_TRADING = "this profile copies a master; switch it to direct trading first"

    def direct_mode(self) -> bool:
        with self._lock:
            return self._virtual is not None

    def watch_direct(self, symbol: str | None) -> None:
        """Tell the feed to quote one extra symbol: the one on the ticket, before anything is
        placed on it, so the person sees a live price while they type."""
        with self._lock:
            self._watch_extra = self._feed_clean(symbol) if symbol else None

    def direct_quote(self, symbol: str) -> Quote | None:
        """The feed's current price for one clean symbol name, or None while unknown."""
        with self._lock:
            tid = self._feed_tid
            snap = self._snapshots.get(tid) if tid is not None else None
            if snap is None:
                return None
            wanted = self._feed_clean(symbol)
            return next((q for q in snap.quotes if q.symbol == wanted), None)

    def direct_feed(self) -> str | None:
        """The terminal currently feeding quotes, or None while there is none."""
        with self._lock:
            return self._feed_tid

    def feed_symbols(self) -> tuple[str, ...]:
        """The feed broker's symbol catalog, as its worker pushed it; empty while unknown."""
        with self._lock:
            tid = self._feed_tid
            if tid is None:
                return ()
            return self._symbol_lists.get(tid, ())

    _RATES_REFRESH_S = 5.0
    """How often the minichart's bars are re-read. Between refreshes the last candle moves
    live off the quotes, so this only paces when a *closed* bar can appear."""

    def direct_rates(self, symbol: str, timeframe: int, count: int = 120) -> RatesResult | None:
        """The feed's bars for the minichart: what is cached when it matches, None until the
        first answer lands. Asking is part of reading -- a read for another symbol or
        timeframe, or an old one, sends the feed worker a fresh :class:`RatesRequest`, so the
        chart's timer is the only clock this needs."""
        with self._lock:
            if self._virtual is None:
                return None
            tid = self._pick_feed()
            if tid is None:
                return None
            symbol = self._feed_clean(symbol)
            key = (symbol, timeframe, tid)
            now = time.monotonic()
            if key != self._rates_asked or now - self._rates_asked_at >= self._RATES_REFRESH_S:
                self._ask_rates(key, count)
            have = self._rates
            if have is not None and (have.symbol, have.timeframe) == (symbol, timeframe):
                return have
            return None

    def _ask_rates(self, key: tuple[str, int, str], count: int) -> None:
        """Send one rates request to the feed worker. Caller must hold the lock."""
        symbol, timeframe, tid = key
        cfg = self._cfg.terminals.get(tid)
        worker = self._workers.get(tid)
        if cfg is None or worker is None:
            return
        req = RatesRequest(
            symbol=symbol,
            timeframe=timeframe,
            count=count,
            symbol_prefix=cfg.symbol_prefix,
            symbol_suffix=cfg.symbol_suffix,
            symbol_map=tuple(sorted(cfg.symbol_map.items())),
        )
        try:
            worker.cmd_q.put_nowait(req)
        except Exception as exc:
            log.debug("could not ask %s for bars: %r", tid[:8], exc)
            return
        self._rates_asked = key
        self._rates_asked_at = time.monotonic()

    def direct_positions(self) -> tuple[Position, ...]:
        with self._lock:
            return self._virtual.positions() if self._virtual is not None else ()

    def direct_orders(self) -> tuple[PendingOrder, ...]:
        with self._lock:
            return self._virtual.orders() if self._virtual is not None else ()

    def direct_copy_status(self) -> dict[int, list[dict[str, Any]]]:
        """Each direct ticket's copies, checked against the slaves' latest snapshots:
        ``{master_ticket: [{"name", "state", "tid", "ticket"}]}`` -- per copy the slave's
        display name, ``"live" | "closed" | "unknown"``, its terminal id (so the page can
        link to the terminal), and the copy's own ticket there (None for a copy with no
        real slave ticket, such as a dry run's).

        The book holds intent; this says what is still real behind it, so the page can
        tell whether closing the ticket would close anything. A believed copy whose
        slave ticket is missing from a *readable* snapshot has been closed on the slave
        on its own (its stop hit there first, or by hand) -- the one state the diff
        itself never notices, because it only ever compares the master against belief.
        ``unknown`` refuses to guess: the slave is disconnected or stale, its positions
        could not be read, or the copy has no real slave ticket -- a dry run's is the
        master's negated (see ``_simulated_result``), and nothing on a broker can vouch
        for a copy that was never sent to one.
        """
        with self._lock:
            now = time.monotonic()
            timeout = self._cfg.heartbeat_timeout_s
            out: dict[int, list[dict[str, Any]]] = {}
            for cfg in self._cfg.slaves:
                copies = self._ledger.believed(cfg.terminal_id)
                if not copies:
                    continue
                snap = self._snapshots.get(cfg.terminal_id)
                received = self._received_at.get(cfg.terminal_id)
                readable = (
                    snap is not None
                    and snap.state is WorkerState.CONNECTED
                    and received is not None
                    and (now - received) <= timeout
                    and snap.open_positions is not None
                )
                open_tickets = (
                    {p.ticket for p in snap.open_positions or ()} if readable and snap else set()
                )
                name = self._cfg.display_name(cfg.terminal_id)
                for copy in copies:
                    real = copy.slave_ticket is not None and copy.slave_ticket >= 0
                    if not readable or not real:
                        state = "unknown"
                    elif copy.slave_ticket in open_tickets:
                        state = "live"
                    else:
                        state = "closed"
                    out.setdefault(copy.master_ticket, []).append(
                        {
                            "name": name,
                            "state": state,
                            "tid": cfg.terminal_id,
                            "ticket": copy.slave_ticket if real else None,
                        }
                    )
            return out

    def place_direct_market(
        self, symbol: str, direction: PositionType, lots: float, sl: float, tp: float = 0.0
    ) -> str | None:
        """Open a direct trade at the feed's price. None means done; otherwise why not.

        The book validates -- the stop is required, on the losing side -- and the next tick
        copies the position to every enabled slave through the ordinary diff, each slave's
        guards gating it exactly as they gate a real master's trade.
        """
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            _, quotes = self._feed_quotes()
            symbol = self._feed_clean(symbol)
            return book.open_market(
                symbol,
                direction,
                lots,
                sl,
                tp,
                quotes.get(symbol),
                now=time.time(),
            )

    def place_direct_pending(
        self,
        symbol: str,
        order_type: OrderType,
        lots: float,
        entry: float,
        sl: float,
        tp: float = 0.0,
        expiration_utc: int = 0,
    ) -> str | None:
        """Rest a direct limit or stop order. None means done; otherwise why not."""
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            _, quotes = self._feed_quotes()
            symbol = self._feed_clean(symbol)
            return book.place_pending(
                symbol,
                order_type,
                lots,
                entry,
                sl,
                tp,
                quotes.get(symbol),
                expiration_utc=expiration_utc,
                now=time.time(),
            )

    def modify_direct(self, ticket: int, sl: float, tp: float) -> str | None:
        """Move a direct position's stop and target; every copy follows on the next tick."""
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            return book.modify_position(ticket, sl, tp)

    def modify_direct_order(
        self, ticket: int, entry: float, sl: float, tp: float, expiration_utc: int = 0
    ) -> str | None:
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            return book.modify_order(ticket, entry, sl, tp, expiration_utc)

    def set_direct_breakeven_at(self, ticket: int, price: float) -> str | None:
        """Arm a breakeven price on a direct position, or clear it with 0. Once the feed
        touches the price, the book moves the stop to the entry and every copy follows."""
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            return book.set_breakeven_at(ticket, price)

    def direct_exit_at_entry(self, ticket: int) -> str | None:
        """Move a direct position's target to its entry plus the copies' costs.

        The breakeven idea pointed the other way: a trade whose reason has gone while it
        sits in loss should leave the moment the price comes back, not wait on a distant
        target. The target moves to the entry plus the dearest live copy's commission and
        slippage buffer -- the same allowance a breakeven stop gets (see
        :func:`mt5copier.breakeven.buffer_distance`) -- so the exit really is at nothing
        lost, on the costliest account. The stop stays where it is, and every copy follows
        the new target through the ordinary diff. None means done; otherwise why not.
        """
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            p = next((x for x in book.positions() if x.ticket == ticket), None)
            if p is None:
                return f"#{ticket} is not an open position in this book"
            sign = 1.0 if p.type is PositionType.BUY else -1.0
            if p.sl and (p.sl - p.price_open) * sign >= 0:
                return (
                    f"#{ticket}'s stop is already at or past its entry ({p.price_open:g}); "
                    "it cannot lose, so there is nothing for an entry exit to do"
                )
            buffer = point = 0.0
            digits = 0
            for cfg in self._cfg.slaves:
                for copy in self._ledger.believed(cfg.terminal_id):
                    if copy.master_ticket != ticket or copy.point <= 0:
                        continue
                    b = buffer_distance(copy, cfg)
                    if b is None or (point > 0 and b < buffer):
                        continue
                    buffer, point, digits = b, copy.point, copy.digits
            if point <= 0:
                return (
                    f"none of #{ticket}'s copies has its costs on record, so the exit "
                    "level cannot be priced; close it by hand instead"
                )
            # At least half a point past the entry, so the rounding lands the target a
            # full point onto the profitable side even when the costs round to nothing --
            # the book refuses a target sitting exactly at the entry.
            tp = stop_past_entry(p.type, p.price_open, max(buffer, point / 2), point, digits)
            _, quotes = self._feed_quotes()
            quote = quotes.get(p.symbol)
            if quote is not None:
                price = quote[0] if p.type is PositionType.BUY else quote[1]
                if (price - tp) * sign >= 0:
                    return (
                        f"#{ticket} is already at or past {tp:g}, the entry plus its "
                        "costs; close it instead to keep what it holds"
                    )
            return book.modify_position(ticket, p.sl, tp)

    def direct_breakevens(self) -> dict[int, float]:
        """Each armed direct breakeven by ticket; empty outside trading mode."""
        with self._lock:
            book = self._virtual
            return book.breakevens() if book is not None else {}

    def close_direct(self, ticket: int) -> str | None:
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            return book.close_position(ticket)

    def partial_close_direct(self, ticket: int, percent: float) -> str | None:
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            return book.partial_close(ticket, percent)

    def cancel_direct(self, ticket: int) -> str | None:
        with self._lock:
            book = self._virtual
            if book is None:
                return self._NOT_TRADING
            return book.cancel_order(ticket)

    def flatten_direct(self) -> int:
        """Close every direct position and cancel every resting order. Returns how many of
        the two together were acted on."""
        with self._lock:
            book = self._virtual
            if book is None:
                return 0
            done = 0
            for p in book.positions():
                if book.close_position(p.ticket) is None:
                    done += 1
            for o in book.orders():
                if book.cancel_order(o.ticket) is None:
                    done += 1
            return done

    def set_mode(self, mode: AppMode) -> str | None:
        """Switch the profile between copying a master and trading directly. None means done.

        Refused while any copy is open or believed, in the spirit of the orphaning rule: the
        other mode's master has different tickets, so copies carried across would read as
        closes on the first tick. The caller still has to save the config.
        """
        with self._lock:
            if mode is self._cfg.mode:
                return None
            self._ensure_store()
            held = len(self._ledger.believed()) + len(self._ledger.believed_orders())
            if held:
                return (
                    f"{copies_word(held)} still open; close them, or give them up by "
                    "unassigning their slaves, before switching modes"
                )
            if self._ledger.inflight_count:
                return "commands are still in flight; try again in a moment"
            if mode is AppMode.TRADING and self._cfg.master is not None:
                # Demote the master alone -- not role_actions.unassign, which sweeps every
                # slave along with it. Here the virtual master takes its place in the same
                # breath, so the slaves keep their roles and copy it instead.
                self._cfg.set_role(self._cfg.master.terminal_id, Role.UNASSIGNED)
            self._cfg.mode = mode
            if mode is AppMode.TRADING:
                self._virtual = VirtualBook(
                    self._virtual_path if self._virtual_path is not None else virtual_book_path()
                )
            else:
                self._virtual = None
                self._snapshots.pop(VIRTUAL_MASTER_ID, None)
                self._received_at.pop(VIRTUAL_MASTER_ID, None)
                self._feed_tid = None
                self._watch_sent = None
                self._rates = None
                self._rates_asked = None
            # The master's identity changed, so every session conclusion reasoned against the
            # old one -- exclusions, continuity, reconciliation -- is void.
            self._ledger.reset_session_state()
            for tid in list(self._cfg.terminals):
                self._forget_reconciliation(tid)
            log.warning(
                "mode switched: this profile now %s",
                "trades directly (virtual master)"
                if mode is AppMode.TRADING
                else "copies a master",
            )
        return None

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
        positions, orders = _undecorated(snap.open_positions, snap.pending_orders, master)

        # Before the links, not after: deciding whether a slave may be copied to now includes
        # reconciling it against the store, so the store has to exist first.
        self._ensure_store()
        if not self._cfg.dry_run:
            self._master_breakeven(master, snap)
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
        self._news_guard(links, positions, orders, snap, dry)
        self._close_guard(links, snap, dry)
        try:
            actions = self._ledger.step(
                positions,
                links,
                master_orders=orders,
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
        self._day.begin_tick(now, positions, orders)
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

            pending = self._guard_pending.pop(
                (action.terminal_id, action.master_ticket, action.kind), None
            )
            if not action.executable:
                reason = action.skip_reason.value if action.skip_reason else None
                guard = self._gate_notes(action)
                self._day.add(action, Outcome.SKIPPED, reason, invert=invert, guard=guard)
                continue
            if action.kind not in DISPATCHABLE_KINDS:
                self._warn_undispatchable(action.kind)
                self._day.add(action, Outcome.NOT_SENT, "this build cannot send it", invert=invert)
                continue
            if dry:
                self._day.add(
                    action,
                    Outcome.DRY_RUN,
                    invert=invert,
                    guard=(pending,) if pending else (),
                    by=pending.rule if pending else "",
                )
                continue
            if cfg is None:
                continue
            slave_snap = self._snapshots.get(action.terminal_id)
            if slave_snap is None:
                self._day.add(
                    action, Outcome.NOT_SENT, "the slave has not reported yet", invert=invert
                )
                continue
            self._day.add(
                action, Outcome.SENDING, invert=invert, by=pending.rule if pending else ""
            )
            self._dispatch(action, cfg, slave_snap, pending)
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
            if key in self._guard_be:
                self._guard_be.discard(key)
                self._guard_close(
                    tid,
                    master_ticket,
                    ActionKind.CLOSE,
                    GuardNote(
                        "A guard",
                        "closed",
                        "the broker refused the breakeven stop it set before the news",
                    ),
                )
                continue
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
            # The master manages some copies: their slave's breakeven leaves them alone.
            copies = [c for c in copies if not c.master_managed]
            if not copies:
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
                booking = (
                    cfg.breakeven_book_percent > 0 and not copy.booked and copy.book_fraction == 0.0
                )
                if booking:
                    self._ledger.set_booking(
                        cfg.terminal_id, copy.master_ticket, cfg.breakeven_book_percent / 100.0
                    )
                self._history.note(
                    cfg.terminal_id,
                    copy.master_ticket,
                    copy.slave_symbol,
                    copy.type.name.lower(),
                    "breakeven",
                    f"reached at {price:g} (fill {opened[ticket]:g}): the stop moves to "
                    f"{level:g} and from now on only tightens"
                    + (
                        f", and {cfg.breakeven_book_percent:g}% is closed to book profit"
                        if booking
                        else ""
                    ),
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

    # --- a guard acting on open copies ------------------------------------------------------

    def _news_guard(
        self,
        links: list[SlaveLink],
        positions: Sequence[Position],
        orders: Sequence[PendingOrder],
        master_snap: Snapshot,
        dry: bool,
    ) -> None:
        """Move to breakeven or close the copies a news rule says to, before the release.

        The one place a guard overrides the master: a copy it closes stays closed while the
        master's trade runs on, and one it moves keeps that stop unless the master's is tighter
        (the breakeven floor, as the slave's own breakeven sets it). Resting copies in the news
        currency are cancelled. Each copy is acted on once in a window; one that cannot be
        acted on yet -- its slave not connected -- is tried again on the next tick.

        Matched on the master's symbol, as the gate does, since a broker's decorated name may
        not read as currencies. Caller must hold the lock.
        """
        symbols = {p.ticket: p.symbol for p in positions} | {o.ticket: o.symbol for o in orders}
        for link in links:
            tid = link.cfg.terminal_id
            acts = self._verdicts.get(tid, NO_VERDICT).news_acts
            if not acts:
                self._news_done.pop(tid, None)
                continue
            if not dry and not link.connected:
                continue
            done = self._news_done.setdefault(tid, set())
            snap = master_snap if dry else self._snapshots.get(tid)
            for copy in self._ledger.believed(tid):
                ticket = copy.master_ticket
                act = self._acting_on(acts, symbols.get(ticket, copy.slave_symbol))
                if act is None or ticket in done or copy.guard_close:
                    continue
                done.add(ticket)
                self._news_act(link.cfg, copy, act, snap, dry)
            for order in self._ledger.believed_orders(tid):
                ticket = order.master_ticket
                act = self._acting_on(acts, symbols.get(ticket, order.slave_symbol))
                if act is None or ticket in done or order.guard_close:
                    continue
                done.add(ticket)
                note = GuardNote(act.title, "cancelled", f"before {act.why}", act.rule_id)
                self._guard_close(tid, ticket, ActionKind.CANCEL, note)

    @staticmethod
    def _acting_on(acts: tuple[NewsAct, ...], symbol: str) -> NewsAct | None:
        """The news rule acting on a copy of ``symbol`` now; a close wins over a breakeven."""
        hits = [a for a in acts if news_hits(a.currencies, symbol)]
        return next((a for a in hits if a.mode == "close"), hits[0] if hits else None)

    def _news_act(
        self,
        cfg: TerminalConfig,
        copy: CopiedPosition,
        act: NewsAct,
        snap: Snapshot | None,
        dry: bool,
    ) -> None:
        """Breakeven or close one copy for a news rule. A breakeven that cannot be set -- the
        copy is in loss, not yet clear of its costs, or its price or costs are unknown -- closes
        it: the point is to be safe before the news, and closing is the side that is."""
        tid, ticket = cfg.terminal_id, copy.master_ticket
        if act.mode != "breakeven":
            note = GuardNote(act.title, "closed", f"before {act.why}", act.rule_id)
            self._guard_close(tid, ticket, ActionKind.CLOSE, note)
            return
        # Live, the copy's own fill and price; a dry run has no copy, so the master's trade
        # stands in for it.
        look = ticket if dry else copy.slave_ticket
        fill = price = None
        if snap is not None and look is not None:
            fill = next((p.price_open for p in snap.open_positions or () if p.ticket == look), None)
            price = next(
                (p.price_current for p in snap.live_positions or () if p.ticket == look), None
            )
        stop = guard_stop(copy, fill, price, cfg) if fill is not None else None
        if stop is None:
            why = (
                "it was in loss or not clear of its costs, so a breakeven stop was impossible"
                if fill is not None and price is not None
                else "its price could not be read, so a breakeven stop could not be set"
            )
            note = GuardNote(act.title, "closed", f"before {act.why}: {why}", act.rule_id)
            self._guard_close(tid, ticket, ActionKind.CLOSE, note)
            return
        if protective_sl(copy.synced_sl, stop, copy.type) == copy.synced_sl:
            return  # Its stop is already at least that safe: nothing to move.
        self._ledger.set_breakeven(tid, ticket, stop)
        self._guard_be.add((tid, ticket))
        note = GuardNote(
            act.title,
            "moved",
            f"its stop to breakeven, {stop:g}, before {act.why}; the master's stop is followed "
            "only when tighter",
            act.rule_id,
        )
        self._guard_pending[(tid, ticket, ActionKind.MODIFY_SLTP)] = note
        self._note_guard_act(cfg, ticket, note)

    def _close_guard(self, links: list[SlaveLink], master_snap: Snapshot, dry: bool) -> None:
        """Close the copies an account rule set to close says to, for as long as it says so.

        ``flatten`` closes every copy and cancels every resting one at once. ``close_worst``
        closes the copy deepest in loss and waits for that close to show in the slave's
        snapshot; the rule is then measured afresh on what is left, so it keeps closing one
        copy at a time until it lets go -- or nothing is left. The slave takes no new trades
        the whole while (every close action also holds it back). Caller must hold the lock.
        """
        for link in links:
            tid = link.cfg.terminal_id
            acts = self._verdicts.get(tid, NO_VERDICT).close_acts
            if not acts:
                self._guard_closed.pop(tid, None)
                continue
            if not dry and not link.connected:
                continue
            act = next((a for a in acts if a.mode == "flatten"), acts[0])
            if act.mode == "flatten":
                for copy in self._ledger.believed(tid):
                    if not copy.guard_close:
                        note = GuardNote(act.title, "closed", act.why, act.rule_id)
                        self._guard_close(tid, copy.master_ticket, ActionKind.CLOSE, note)
                for order in self._ledger.believed_orders(tid):
                    if not order.guard_close:
                        note = GuardNote(act.title, "cancelled", act.why, act.rule_id)
                        self._guard_close(tid, order.master_ticket, ActionKind.CANCEL, note)
                continue
            # Live, each copy's P/L is on the slave's snapshot by its slave ticket; a dry run
            # has no copies on the broker, so the master's trades stand in for them.
            self._close_worst(tid, act, master_snap if dry else self._snapshots.get(tid), dry)

    def _close_worst(self, tid: str, act: CloseAct, snap: Snapshot | None, dry: bool) -> None:
        """Close the one copy deepest in loss, unless an earlier close is still in flight."""
        copies = self._ledger.believed(tid)
        if any(c.guard_close for c in copies):
            return  # a close is on its way; what is left is measured once it lands
        closed = self._guard_closed.get(tid)
        if closed:
            closed &= {p.ticket for p in (snap.open_positions if snap else None) or ()}
            if closed:
                return  # the last close has not reached the snapshot yet
        pnl: dict[int, float] = {}
        for live in (snap.live_positions if snap else None) or ():
            if live.profit is not None:
                pnl[live.ticket] = live.profit + (live.swap or 0.0)
        worst: CopiedPosition | None = None
        worst_pnl = 0.0
        for copy in copies:
            look = copy.master_ticket if dry else copy.slave_ticket
            value = pnl.get(look) if look is not None else None
            if value is None:
                continue
            if worst is None or value < worst_pnl:
                worst, worst_pnl = copy, value
        if worst is None:
            return  # no copy's P/L is known yet; tried again on the next tick
        note = GuardNote(
            act.title,
            "closed",
            f"the worst copy, {worst_pnl:,.2f} floating: {act.why}",
            act.rule_id,
        )
        self._guard_close(tid, worst.master_ticket, ActionKind.CLOSE, note)
        if not dry and worst.slave_ticket is not None:
            self._guard_closed.setdefault(tid, set()).add(worst.slave_ticket)

    def _guard_close(self, tid: str, ticket: int, kind: ActionKind, note: GuardNote) -> None:
        """Close or cancel one copy for a guard; the next diff derives it."""
        if not self._ledger.request_close(tid, ticket):
            return
        self._guard_pending[(tid, ticket, kind)] = note
        cfg = self._cfg.terminals.get(tid)
        if cfg is not None:
            self._note_guard_act(cfg, ticket, note)

    def _note_guard_act(self, cfg: TerminalConfig, ticket: int, note: GuardNote) -> None:
        name = self._cfg.display_name(cfg.terminal_id)
        log.warning("guards: #%s on %s: %s", ticket, name, note.text())

    # --- the master's own breakeven ---------------------------------------------------------

    def _master_breakeven(self, master: TerminalConfig, snap: Snapshot) -> None:
        """Move the master's own stop to breakeven, and book part of it, as its settings ask.

        The only trading the copier does on the master, and only when that is switched on.
        Each trade's R is the distance to the first stop it was seen with; once it is that many
        R in profit its worker is asked to set the stop (see ``Executor._to_breakeven``), which
        never loosens one. That is done once per trade, remembered across restarts: after it,
        the stop is the person's again, and nothing puts it back if they move it. Slaves the
        master manages follow the move and the booking like any other.

        Caller must hold the lock, with the store open.
        """
        if master.breakeven_mode is BreakevenMode.OFF:
            return
        if snap.open_positions is None or snap.live_positions is None:
            return
        store = self._store
        assert store is not None
        tid = master.terminal_id
        known = store.master_trades(tid)
        store.forget_master_trades(tid, keep=[p.ticket for p in snap.open_positions])
        current = {p.ticket: p.price_current for p in snap.live_positions}
        now = time.monotonic()
        for p in snap.open_positions:
            trade = known.get(p.ticket) or MasterTrade(tid, p.ticket)
            if trade.risk_distance <= 0 and p.sl and not trade.breakeven_done:
                trade = store.remember_master_trade(
                    replace(trade, risk_distance=abs(p.price_open - p.sl))
                )
            elif p.ticket not in known:
                store.remember_master_trade(trade)
            if now < self._master_be_wait.get(p.ticket, 0.0):
                continue
            if trade.breakeven_done:
                if master.breakeven_book_percent > 0 and not trade.booked:
                    sent = self._send_to_master(
                        master,
                        p,
                        ActionKind.BOOK,
                        close_fraction=master.breakeven_book_percent / 100.0,
                    )
                    if sent:
                        # Marked as it goes, so a restart while it is out does not send it again.
                        store.remember_master_trade(replace(trade, booked=True))
                continue
            price = current.get(p.ticket)
            if price is None or not master_breakeven_due(
                p.type, p.price_open, price, trade.risk_distance, master.breakeven_trigger_r
            ):
                continue
            self._send_to_master(master, p, ActionKind.MODIFY_SLTP, to_breakeven=True)

    def _send_to_master(
        self, master: TerminalConfig, p: Position, op: ActionKind, **kw: object
    ) -> bool:
        """Put one command for the master's own position on its queue, unless one is out.
        True if it went."""
        key = (master.terminal_id, p.ticket, op)
        if key in self._pending_cmd:
            return False
        worker = self._workers.get(master.terminal_id)
        if worker is None:
            return False
        command_id = next(self._command_ids)
        cmd = OrderCommand(
            command_id=command_id,
            op=op,
            master_ticket=p.ticket,
            master_symbol=p.symbol,
            direction=p.type,
            entry=p.price_open,
            sl=p.sl,
            tp=p.tp,
            master_volume=p.volume,
            slave_ticket=p.ticket,
            commission_per_lot=master.commission_per_lot_round_turn,
            slippage_points=master.slippage_buffer_points,
            deviation_points=master.max_deviation_points,
            magic=COPIER_MAGIC,
            comment="breakeven",
            allow_live=master.allow_live_trading,
            **kw,  # type: ignore[arg-type]
        )
        try:
            worker.cmd_q.put_nowait(cmd)
        except Exception as exc:
            log.error("could not send the master's %s for #%s: %r", op, p.ticket, exc)
            self._master_be_wait[p.ticket] = time.monotonic() + _MASTER_BE_RETRY_S
            return False
        self._pending_cmd[key] = (command_id, time.monotonic())
        log.info(
            "-> master %s #%s (cmd %s)",
            "breakeven" if op is ActionKind.MODIFY_SLTP else "booking",
            p.ticket,
            command_id,
        )
        return True

    def _on_master_result(self, master: TerminalConfig, result: OrderResult) -> None:
        """The answer to the master's own breakeven or booking. Caller must hold the lock."""
        key = (result.terminal_id, result.master_ticket, result.op)
        expected = self._pending_cmd.get(key)
        if result.ok or not result.retryable:
            # "Not past the level yet" is the routine answer while a trade hovers about its
            # trigger, and is not a failure to count.
            self._record_result(result)
        if expected is None or expected[0] != result.command_id:
            log.error(
                "dropping the master's result for #%s: it answers command %s",
                result.master_ticket,
                result.command_id,
            )
            return
        del self._pending_cmd[key]
        store = self._ensure_store()
        trade = store.master_trades(master.terminal_id).get(result.master_ticket)
        ticket = result.master_ticket
        if result.op is ActionKind.BOOK:
            if result.ok:
                log.info("master #%s: booked %s lots at breakeven", ticket, result.filled_volume)
            else:
                log.error("master #%s: the booking at breakeven failed: %s", ticket, result.error)
            return
        if result.ok:
            self._master_be_wait.pop(ticket, None)
            if trade is not None and not result.target_gone:
                store.remember_master_trade(replace(trade, breakeven_done=True))
            log.info(
                "master #%s: breakeven %s at %s; from now on its stop is left to you",
                ticket,
                "already in place" if result.outcome == "already past breakeven" else "set",
                result.price,
            )
            return
        wait = _MASTER_BE_RETRY_S if result.retryable else _MASTER_BE_GIVE_UP_S
        self._master_be_wait[ticket] = time.monotonic() + wait
        (log.info if result.retryable else log.error)(
            "master #%s: breakeven not set (%s); asked again in %.0fs", ticket, result.error, wait
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
            if price and copy.master_managed:
                return (
                    "the master manages this copy, so it follows the master's stop rather than "
                    "a breakeven of its own"
                )
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

    @staticmethod
    def _decor(cfg: TerminalConfig) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        """This slave's symbol decoration as the guards take it: prefix, suffix, renames."""
        return (cfg.symbol_prefix, cfg.symbol_suffix, tuple(sorted(cfg.symbol_map.items())))

    def _class_lots_limit(
        self, cfg: TerminalConfig, verdict: Verdict, action: Action, snap: Snapshot | None
    ) -> tuple[float, str, str, str, str] | None:
        """One lots ceiling for this trade under the asset-class rules, or None for no limit.

        The tightest applicable rule wins: its cap for the symbol's class as it stands, less
        the class's open lots on this slave for the ``total`` scope. Open lots that cannot be
        counted follow the margin caps' philosophy -- a warn-only rule lets the trade through
        as one on its own, any other mode refuses it, because sending unmeasured is the
        unsafe side. Returns ``(ceiling, mode, rule title, rule id, what was read)``; the
        last always names the class, so a misfiled symbol is visible, never silent.
        """
        if not verdict.class_lots:
            return None
        catalog = self._catalog.current()
        clas = catalog.classify(action.master_symbol)
        word = CLASS_WORDS.get(clas, clas).lower()
        read = f"{action.master_symbol} → {word}"
        best: tuple[float, str, str, str, str] | None = None
        for rule in verdict.class_lots:
            cap = rule.cap_for(clas)
            if cap is None:
                continue
            if rule.scope == "total":
                open_lots = self._class_open_lots(cfg, snap, clas, catalog)
                if open_lots is None:
                    ceiling = cap if rule.mode == "warn" else 0.0
                    allowed = f"{read}: its open lots could not be counted"
                else:
                    ceiling = max(0.0, cap - open_lots)
                    allowed = f"{read}: {open_lots:g} of {cap:g} lots already open"
            else:
                ceiling = cap
                allowed = f"{read}, capped at {cap:g} lots a trade"
            pick = (ceiling, rule.mode, rule.title, rule.rule_id, allowed)
            if best is None or pick[0] < best[0]:
                best = pick
        return best

    @staticmethod
    def _class_open_lots(
        cfg: TerminalConfig, snap: Snapshot | None, clas: str, catalog: Catalog
    ) -> float | None:
        """All open lots of one class on this slave, its decoration translated off first --
        a rename reversed, then the configured prefix and suffix -- so the catalog is asked
        about base names. None when the positions cannot be read."""
        if snap is None or snap.open_positions is None:
            return None
        back = {slave.upper(): master for master, slave in cfg.symbol_map.items()}
        total = 0.0
        for position in snap.open_positions:
            base = back.get(position.symbol.upper()) or undecorate(
                position.symbol, cfg.symbol_prefix, cfg.symbol_suffix
            )
            if catalog.classify(base) == clas:
                total += position.volume
        return total

    def _dispatch(
        self,
        action: Action,
        cfg: TerminalConfig,
        snap: Snapshot,
        pending: GuardNote | None = None,
    ) -> None:
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
        risk, risk_note = self._capped_risk(action, cfg, risk, verdict)
        notes = tuple(n for n in (risk_note, self._stop_note(action, cfg, verdict), pending) if n)
        opening = action.kind in (ActionKind.OPEN, ActionKind.PLACE)
        constraints: tuple[TradeConstraint, ...] = ()
        if opening:
            # Lots first: it is the plain ceiling, and each later fit measures the lots the
            # one before let through.
            lots_limit = self._class_lots_limit(cfg, verdict, action, snap)
            if lots_limit is not None:
                constraints += (TradeConstraint("lots", *lots_limit),)
            for kind, cap in (
                ("margin", verdict.max_margin),
                ("total_margin", verdict.max_total_margin),
            ):
                if cap is not None:
                    constraints += (
                        TradeConstraint(kind, cap.amount, cap.mode, cap.rule, cap.rule_id),
                    )

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
            expiration_utc=action.expiration_utc,
            master_volume=action.master_volume,
            sizing_basis=action.sizing_basis,
            lot_multiple=action.lot_multiple,
            risk_amount=risk,
            commission_per_lot=cfg.commission_per_lot_round_turn,
            slippage_points=cfg.slippage_buffer_points,
            max_lot=cfg.max_lot,
            deviation_points=cfg.max_deviation_points,
            pending_fallback=cfg.pending_fallback,
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
            constraints=constraints,
            # Direct trades only: the ticket's target is an rr intent, so each worker nudges
            # its copy's target out by (rr + 1) x its own commission to keep the realized
            # ratio. A real master's target is that trader's own level and goes untouched.
            tp_net_rr=self._virtual is not None
            and action.kind
            in (ActionKind.OPEN, ActionKind.PLACE, ActionKind.MODIFY_SLTP, ActionKind.MODIFY_ORDER),
        )

        # Before sending, because an order that cannot be sent is answered on the spot.
        self._day.on_sent(
            action,
            command_id,
            sent_at=time.monotonic(),
            guard=notes,
        )
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
    ) -> tuple[float, GuardNote | None]:
        """The risk budget to size a new trade from, after its guards' risk cap, and what the
        cap did to it, if anything.

        A cap set to decline never gets here: the slave's budget is the same for every trade
        sized from its stop, so the gate refused the trade already, where a dry run shows it
        too. Set to shrink, the budget comes down to the cap, which sizes the trade to fit; set
        to warn, it is said and the trade goes as sized.
        """
        cap = verdict.max_risk
        if cap is None or action.kind not in (ActionKind.OPEN, ActionKind.PLACE):
            return risk, None
        if risk <= cap.amount:
            return risk, None
        name = self._cfg.display_name(cfg.terminal_id)
        if cap.mode == "shrink":
            note = GuardNote(
                cap.rule,
                "shrunk",
                f"sized to risk {cap.amount:,.2f}, not the slave's {risk:,.2f}",
                cap.rule_id,
            )
            log.warning("guards: #%s on %s: %s", action.master_ticket, name, note.text())
            return cap.amount, note
        note = GuardNote(
            cap.rule,
            "warned",
            f"risks {risk:,.2f}, over the {cap.amount:,.2f} allowed; sent as sized",
            cap.rule_id,
        )
        log.warning("guards: #%s on %s: %s", action.master_ticket, name, note.text())
        return risk, note

    def _stop_note(self, action: Action, cfg: TerminalConfig, verdict: Verdict) -> GuardNote | None:
        """What a stop rule set only to warn says about a new trade going out with no stop.

        Set to decline, the gate refused the trade already; set to warn, it goes, and is said.
        """
        if verdict.stop_warning is None or action.sl != 0.0:
            return None
        if action.kind not in (ActionKind.OPEN, ActionKind.PLACE):
            return None
        note = GuardNote(
            verdict.stop_warning,
            "warned",
            "it has no stop loss; sent anyway",
            verdict.stop_warned_by,
        )
        name = self._cfg.display_name(cfg.terminal_id)
        log.warning("guards: #%s on %s: %s", action.master_ticket, name, note.text())
        return note

    def _gate_notes(self, action: Action) -> tuple[GuardNote, ...]:
        """Which guard declined a trade the gate refused, read off the slave's verdict."""
        reason = action.skip_reason
        verdict = self._verdicts.get(action.terminal_id, NO_VERDICT)
        key = self._account_of(action.terminal_id)
        title = self._guards.title
        if reason is SkipReason.GUARD_HOLDS:
            held = zip(verdict.held_by, verdict.held_back, strict=True)
            notes = tuple(GuardNote(title(key, r), "declined", why, r) for r, why in held)
            return notes or (GuardNote("A guard", "declined", reason.value),)
        if reason is SkipReason.GUARD_NEWS:
            return tuple(
                GuardNote(title(key, r), "declined", verdict.news_why, r)
                for r in verdict.news_by or ("",)
            )
        if reason is SkipReason.GUARD_NEEDS_STOP:
            name = verdict.stop_required or "A guard"
            return (GuardNote(name, "declined", "it has no stop loss", verdict.stop_by),)
        if reason is SkipReason.GUARD_RISK and verdict.max_risk is not None:
            cap = verdict.max_risk
            why = f"this slave risks more on a trade than the {cap.amount:,.2f} allowed"
            return (GuardNote(cap.rule, "declined", why, cap.rule_id),)
        return ()

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
        master = self._cfg.master
        if master is not None and result.terminal_id == master.terminal_id:
            self._on_master_result(master, result)
            return
        if result.ok and result.op in (ActionKind.OPEN, ActionKind.PLACE):
            # Counted before the correlation check below: even an answer the ledger stopped
            # waiting for placed a real trade at the broker, and the trade-count rules count
            # placements, not beliefs.
            self._guards.record_open(
                self._account_of(result.terminal_id),
                pending=result.op is ActionKind.PLACE and not result.filled_at_market,
                now=time.time(),
            )
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
        for note in result.guard:
            log.warning(
                "guards: #%s on %s: %s",
                result.master_ticket,
                self._cfg.display_name(result.terminal_id),
                note.text(),
            )
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
            if outcome.guard:
                account = self._account_of(outcome.terminal_id) or ""
                for note in outcome.guard:
                    self._history.record_guard(
                        GuardAction(
                            ts=outcome.ts if outcome.ts is not None else head.ts,
                            account=account,
                            terminal_id=outcome.terminal_id,
                            rule_id=note.rule_id,
                            rule=note.rule,
                            action=note.action,
                            detail=note.detail,
                            master_ticket=head.master_ticket,
                            symbol=head.symbol,
                            label=head.label,
                        )
                    )
        except Exception:
            log.exception("could not record a step in the trade history")

    def guard_actions(self, terminal_id: str, *, since: float | None = None) -> list[GuardAction]:
        """What the guards did on the account logged into this terminal, newest first. Read
        from disk, so it reaches back past this session."""
        with self._lock:
            key = self._account_of(terminal_id)
        if key is None:
            return []
        return self._history.guard_actions(key, since=since)

    def recent_guard_actions(self, since: float) -> list[GuardAction]:
        """What the guards did on any account since ``since``, newest first."""
        return self._history.recent_guard_actions(since=since)

    def recent_problems(self, since: float) -> list[Step]:
        """Copies that went wrong on any slave since ``since`` with no guard behind them,
        newest first: refusals, link failures, copies never answered."""
        return self._history.recent_problems(since=since)

    def news_calendar(self) -> NewsCalendar:
        """The calendar the news rules read; the News page reads the same one."""
        return self._calendar

    def _record_guard(self, key: str | None, terminal_id: str, note: GuardNote) -> None:
        """Something a guard did to the slave as a whole, rather than to one trade."""
        if key is None:
            return
        self._history.record_guard(
            GuardAction(
                time.time(),
                key,
                terminal_id,
                note.rule_id,
                note.rule,
                note.action,
                note.detail,
            )
        )

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
            account_key(snap.login, snap.server),
            snap,
            time.time(),
            sizing_risk=risk,
            decor=self._decor(cfg),
        )
        before = self._verdicts.get(tid, NO_VERDICT)
        self._verdicts[tid] = verdict
        name = self._cfg.display_name(tid)
        self._record_changes(account_key(snap.login, snap.server), tid, before, verdict)
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

    def _record_changes(self, key: str | None, tid: str, before: Verdict, now: Verdict) -> None:
        """Keep the moments a rule started or stopped holding the slave back, and a warning
        first given: the slave-wide half of what the guards page lists."""
        title = self._guards.title
        started = zip(now.held_by, now.held_back, strict=True)
        for rule_id, why in started:
            if rule_id not in before.held_by:
                note = GuardNote(title(key, rule_id), "stopped new trades", why, rule_id)
                self._record_guard(key, tid, note)
        for rule_id in now.news_by:
            if rule_id not in before.news_by:
                note = GuardNote(title(key, rule_id), "stopped new trades", now.news_why, rule_id)
                self._record_guard(key, tid, note)
        for rule_id in (*before.held_by, *before.news_by):
            if rule_id not in now.held_by and rule_id not in now.news_by:
                note = GuardNote(title(key, rule_id), "let new trades through", "", rule_id)
                self._record_guard(key, tid, note)
        for rule_id, line in zip(now.warned_by, now.warnings, strict=True):
            if rule_id not in before.warned_by:
                note = GuardNote(title(key, rule_id), "warned", line, rule_id)
                self._record_guard(key, tid, note)

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
            decor = self._decor(cfg) if cfg is not None else None
            return self._guards.report(key, snap, time.time(), sizing_risk=risk, decor=decor)

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
                SlaveLink(
                    cfg=cfg,
                    connected=connected,
                    gate=self._gate(cfg.terminal_id, snap),
                    master_manages=self._cfg.manages(cfg),
                )
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

    def copies_on_real_account(self, terminal_id: str) -> int:
        """How many copies this slave holds when its account is not known to be a demo -- the
        ones the demo-only guard will refuse to close or move once its override is off."""
        with self._lock:
            snap = self._snapshots.get(terminal_id)
            if snap is not None and snap.trade_mode == ACCOUNT_TRADE_MODE_DEMO:
                return 0
            return len(self._open_copies(terminal_id) or [])

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

        Only a change that stops copies being managed at all: unassigning the terminal, or
        disabling it, which stops its worker (or, for the master, stops anything being
        mirrored). A slave only ever stakes its own; every slave's copies hang off the master,
        so unassigning or disabling it sweeps them all along (see :func:`role_actions.unassign`).

        **A settings edit stakes nothing.** Each copy keeps what it was opened with -- its
        size, symbol and direction, read back from the copy rather than the setting, so even
        ``invert`` only reaches new trades -- and the settings that act on open copies (copying
        stops, partials and pending orders, the breakeven and its buffer) are meant to.
        """
        cfg = self._cfg.terminals[terminal_id]
        losing = new is None or (cfg.enabled and not new.enabled)
        if not losing:
            return []
        if cfg.role is not Role.MASTER:
            return [terminal_id]
        return [t.terminal_id for t in self._cfg.slaves]

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

        **A change that would stop copies being managed is only made by giving them up**: the
        copier stops managing them -- nothing closes them when the master does, moves their
        stops, or mirrors a partial close -- and they stay open on the slave. Only disabling
        does that (see :meth:`_stake_ids`); any other edit keeps the copies managed. See
        :meth:`_give_up_agreed` for what ``orphan`` must hold.

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

    def logins(self) -> dict[str, int | None]:
        """The account each terminal is logged into: the one its latest snapshot names, else the
        one it was last known to be logged into. The login alone, as a setup keeps it."""
        with self._lock:
            return self._logins()

    def _logins(self) -> dict[str, int | None]:
        """Caller must hold the lock."""
        found = setups_mod.logins_of(self._cfg)
        for tid, snap in self._snapshots.items():
            if tid in found and snap.login is not None:
                found[tid] = snap.login
        return found

    def setup_stake(self, setup: Setup) -> dict[str, list[OpenCopy]] | None:
        """The open copies loading this setup would give up, by slave, or None if any of them
        cannot be read. An empty dict means there is nothing to ask."""
        with self._lock:
            return self._setup_stake(setup)

    def _setup_stake(self, setup: Setup) -> dict[str, list[OpenCopy]] | None:
        """Caller must hold the lock."""
        stake: dict[str, list[OpenCopy]] = {}
        for tid in setups_mod.losing(setup, self._cfg, self._logins()):
            held = self._open_copies(tid)
            if held is None:
                return None
            if held:
                stake[tid] = held
        return stake

    def load_setup(
        self,
        setup: Setup,
        *,
        orphan: Mapping[str, Collection[int]] | None = None,
        leave_out: Collection[str] = (),
        go_live: bool = False,
    ) -> str | None:
        """Put a saved setup back: its roles, every terminal's settings, copying on or off and
        the dry run or live mode, as it was saved. None means done; otherwise why not, with
        nothing changed.

        **Going live takes** ``go_live``, given only once the person has typed ``LIVE``, as the
        copier's own settings ask; a setup that would turn a dry run into live copying without
        it is refused. Nor is the mode switched while commands are out, whose answers would be
        matched against the other set of beliefs. The live-trading override is never turned on
        (see :func:`setups.restore_settings`).

        The same rule as :meth:`unassign`, for the same reason: a slave taken off its master --
        or left on while the master changes under it, or logged into another account -- has its
        copies given up, and only if the person agreed to exactly those. One that keeps its
        master and its account keeps its copies.

        **A terminal is never given a role on an account the setup does not name.** One that has
        to be logged into its account first must be in ``leave_out``, and is left unassigned:
        the app logs it in and then loads the setup again (see :func:`setups.apply_roles`).

        The caller still has to save the config and :meth:`sync`.
        """
        with self._lock:
            logins = self._logins()
            found = setups_mod.problems(setup, self._cfg, logins)
            if found:
                return "; ".join(found)
            unswitched = [
                self._cfg.display_name(tid)
                for tid in setups_mod.switches(setup, logins)
                if tid not in leave_out
            ]
            if unswitched:
                return f"{', '.join(unswitched)} is not logged into the setup's account"
            if setups_mod.goes_live(setup, self._cfg) and not go_live:
                return "it copies live, which needs LIVE typed to switch from the dry run"
            switching = setup.dry_run is not None and setup.dry_run != self._cfg.dry_run
            if switching and self._ledger.inflight_count:
                return "commands are still in flight; try again in a moment"
            refusal = self._give_up_agreed(self._setup_stake(setup), orphan)
            if refusal is not None:
                return refusal
            setups_mod.apply_roles(setup, self._cfg, leave_out)
            setups_mod.restore_settings(setup, self._cfg)
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
        for copy in let_go:
            # Its history would otherwise go on saying "open": nothing after this is recorded
            # for it on this slave, because nothing here manages it any more.
            self._history.note(
                terminal_id,
                copy.master_ticket,
                copy.symbol,
                "",
                ORPHANED,
                (
                    "settings changed: the copier no longer moves or cancels this order, "
                    "which stays resting on the slave"
                    if copy.resting
                    else "settings changed: the copier no longer closes, moves or partly "
                    "closes this copy, which stays open on the slave"
                ),
            )
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
        self._settle_management(self._store)
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

    def _settle_management(self, store: MappingStore) -> None:
        """Decide who manages each copy recorded before that was kept, once: as a trade
        opened now would be, from the settings as they are. A row whose terminal is no longer
        a slave is left to the master, which is what a slave starts out with."""

        def decide(terminal_id: str) -> bool:
            cfg = self._cfg.terminals.get(terminal_id)
            return self._cfg.manages(cfg) if cfg is not None else True

        settled = store.settle_management(decide)
        if settled:
            log.info("decided who manages %d copy record(s) from before that was kept", settled)

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
                        claim_refused=self._claim_refused.get(tcfg.terminal_id),
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
