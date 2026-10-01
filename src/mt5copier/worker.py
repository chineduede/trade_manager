"""One MT5 terminal connection, owned by one OS process.

The ``MetaTrader5`` package keeps its connection in process-global state: ``initialize()`` and
every subsequent call operate on a single implicit session per process. There is no handle to
pass around and no way to hold two sessions at once, so N terminals require N processes. This
module is the body of one such process.

``MetaTrader5`` is imported lazily inside :func:`run_worker` rather than at module scope, so
that the supervisor -- which imports this module to get at ``run_worker`` -- never loads the
extension into the parent process.

Two behaviours here are direct responses to gaps in ``zele-mt5``:

* **Reconnection.** That project connected once in a constructor and had no retry, no backoff
  and no health check, so a dropped broker connection killed the worker permanently. Here the
  connection is a loop with a backoff schedule and continuous liveness checks.
* **Reads never tear down the session.** Its ``get_acct_info()`` called ``mt5.shutdown()`` when
  a read failed, destroying the process's connection as a side effect of a query. Here a failed
  read only marks state; teardown happens exclusively in the reconnect path.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from .discovery import find_terminal_pid
from .executor import UNREADABLE, Executor, LivePosition, ReadFailure
from .logging_setup import configure, get_logger
from .protocol import (
    Command,
    CommandKind,
    OrderCommand,
    OrderResult,
    OrderType,
    PendingOrder,
    Position,
    PositionType,
    Snapshot,
    WorkerState,
)
from .sizing import SymbolFacts

if TYPE_CHECKING:
    from multiprocessing.queues import Queue

log = get_logger(__name__)

# Guard against a hung terminal blocking the worker forever. MT5's own default is 60s.
_INIT_TIMEOUT_MS = 20_000


class _Session:
    """Thin wrapper over the process-global MT5 module.

    Exists to keep the polling loop readable and to give tests a seam: the loop only ever talks
    to this object, so a fake session can be injected without patching module globals.
    """

    def __init__(self, mt5: Any, exe_path: Path) -> None:
        self._mt5 = mt5
        self._exe_path = exe_path
        self.connected = False
        self._launched_pid: int | None = None
        """The terminal's pid, set only when *this* ``connect()`` call was the one that started
        it -- i.e. nothing was running at ``exe_path`` the instant before. None means either we
        are not connected, or the terminal was already running, which makes it the user's to
        close, not ours. Re-derived on every connect, so a terminal we launched that later
        crashes and gets relaunched by hand is correctly no longer considered ours."""

    def connect(self) -> tuple[bool, str | None]:
        """Call ``initialize(path=...)``. Returns (ok, error message).

        This is the only place that may launch a terminal: per the MT5 docs, ``initialize()``
        starts the terminal if it is not already running. That is why discovery never calls it
        and why this runs only for terminals the user has explicitly designated.

        Notes whether this call is the one that launched it, by checking just before: an
        instance already running is the user's own, not ours to ever close later.
        """
        already_running = find_terminal_pid(self._exe_path) is not None
        try:
            ok = self._mt5.initialize(str(self._exe_path), timeout=_INIT_TIMEOUT_MS)
        except Exception as exc:  # the extension can raise, not just return False
            return False, f"initialize() raised: {exc!r}"
        if not ok:
            return False, f"initialize() failed: {self._last_error()}"
        self.connected = True
        self._launched_pid = None if already_running else find_terminal_pid(self._exe_path)
        return True, None

    def disconnect(self, *, close_terminal: bool = False) -> None:
        """Close the MT5 session. With ``close_terminal=True``, also close the terminal
        process itself -- but only the one this session launched.

        Plain disconnect (the reconnect path) must never close the terminal: it would kill the
        very instance a moment before reconnecting to it. Only a caller that is done with this
        terminal for good passes ``close_terminal=True``.
        """
        if self.connected:
            try:
                self._mt5.shutdown()
            except Exception as exc:
                log.debug("shutdown() raised, ignoring: %r", exc)
            finally:
                self.connected = False
        if close_terminal:
            self._close_launched_terminal()

    def _close_launched_terminal(self) -> None:
        pid = self._launched_pid
        self._launched_pid = None
        if pid is None:
            return
        try:
            proc = psutil.Process(pid)
            if str(proc.exe()).lower() != str(self._exe_path).lower():
                # The pid was recycled for something else entirely; refuse to touch it.
                log.warning("pid %s is no longer the terminal; not closing", pid)
                return
            proc.terminate()
            log.info("closed terminal pid=%s (we launched it)", pid)
        except psutil.NoSuchProcess:
            pass  # already gone
        except Exception as exc:
            log.debug("could not close terminal pid=%s: %r", pid, exc)

    def _last_error(self) -> str:
        try:
            return str(self._mt5.last_error())
        except Exception:
            return "(last_error unavailable)"

    def _empty_or_unknown(self, what: str, total_attr: str) -> tuple[()] | None:
        """Disambiguate the ``None`` that ``positions_get()`` / ``orders_get()`` return.

        MT5 uses ``None`` for two entirely different answers -- "the call failed" and, on some
        builds, "there is nothing to return" -- and the two must not be conflated: an empty
        list is acted on (close the copies, cancel the copies) while an unknown one is skipped.
        Guessing wrong in one direction cancels live orders; in the other it silently freezes
        the copier.

        Rather than interpret ``last_error()``, ask the matching count function. A count of
        zero settles it; anything else, including a count that will not read, stays unknown.
        """
        try:
            # Resolved inside the try: an extension that lacks the call at all must read as
            # unknown, not raise out of the poll and take the worker down with it.
            total = getattr(self._mt5, total_attr)()
        except Exception as exc:
            log.debug("%s count unavailable after a None read: %r", what, exc)
            return None
        if total == 0:
            return ()
        log.debug("%s read returned None while %r are open; treating as unknown", what, total)
        return None

    # --- what the executor needs ----------------------------------------------------------
    #
    # Thin, forgiving wrappers: each returns None rather than raising, because a missing or
    # misbehaving read must become a refusal to trade, not an exception that kills the worker.

    def account_trade_mode(self) -> int | None:
        """``account_info().trade_mode``. None when it cannot be read, which the executor
        treats as "not a demo"."""
        try:
            account = self._mt5.account_info()
        except Exception as exc:
            log.debug("account_info() raised while checking trade mode: %r", exc)
            return None
        mode = getattr(account, "trade_mode", None) if account is not None else None
        try:
            return None if mode is None else int(mode)
        except (TypeError, ValueError):
            return None

    def symbol_names(self) -> list[str]:
        try:
            symbols = self._mt5.symbols_get()
        except Exception as exc:
            log.debug("symbols_get() raised: %r", exc)
            return []
        if not symbols:
            return []
        return [str(s.name) for s in symbols if getattr(s, "name", None)]

    def symbol_facts(self, name: str) -> SymbolFacts | None:
        """``symbol_info``, selecting the symbol into Market Watch if it isn't already visible.

        ``.visible`` rather than "was ``symbol_info`` ``None``" is the right check -- an
        unselected symbol does not reliably answer with ``None``, some brokers hand back a
        non-None struct with zeroed fields instead, a filling mask of zero being the most
        dangerous one. Re-reading after a *successful* select is for the same reason: the
        struct read before selecting can still be the stale, zeroed one.

        If the select call itself reports failure, one retry after a short wait follows rather
        than failing outright -- a terminal that just can't act on it instantly is not the same
        as a symbol that does not exist.
        """
        try:
            info = self._mt5.symbol_info(name)
            if info is None:
                return None
            if not info.visible:
                if self._mt5.symbol_select(name, True):
                    info = self._mt5.symbol_info(name)
                else:
                    log.warning("symbol_select(%r) failed; retrying after a short wait", name)
                    time.sleep(2.0)
                    info = self._mt5.symbol_info(name)
        except Exception as exc:
            log.debug("symbol_info(%r) raised: %r", name, exc)
            return None
        if info is None:
            return None
        try:
            return SymbolFacts(
                name=str(getattr(info, "name", name)),
                volume_min=float(info.volume_min),
                volume_max=float(info.volume_max),
                volume_step=float(info.volume_step),
                point=float(getattr(info, "point", 0.0) or 0.0),
                digits=int(getattr(info, "digits", 5) or 5),
                filling_mask=int(getattr(info, "filling_mode", 0) or 0),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            log.warning("unusable symbol_info for %r: %s", name, exc)
            return None

    def tick(self, name: str) -> tuple[float, float] | None:
        """Current ``(bid, ask)``. Re-read on every reprice; never cached."""
        try:
            quote = self._mt5.symbol_info_tick(name)
        except Exception as exc:
            log.debug("symbol_info_tick(%r) raised: %r", name, exc)
            return None
        if quote is None:
            return None
        try:
            return float(quote.bid), float(quote.ask)
        except (AttributeError, TypeError, ValueError):
            return None

    def loss_per_lot(
        self, direction: PositionType, symbol: str, entry: float, sl: float
    ) -> float | None:
        """What one lot loses between two prices, in the account currency.

        Asks the terminal via ``order_calc_profit`` rather than deriving it from tick values:
        it is exact, and it handles cross-currency instruments without us converting anything.
        """
        try:
            profit = self._mt5.order_calc_profit(int(direction), symbol, 1.0, entry, sl)
        except Exception as exc:
            log.debug("order_calc_profit(%r) raised: %r", symbol, exc)
            return None
        if profit is None:
            return None
        try:
            return abs(float(profit))
        except (TypeError, ValueError):
            return None

    def position(self, ticket: int) -> LivePosition | None | ReadFailure:
        """``(symbol, direction, volume)`` for one open position, read live.

        Closing, reducing and modifying use the terminal's own figures rather than whatever we
        last recorded. Our record can be stale -- the user may have closed part of it by hand
        -- and a stale volume produces an INVALID_VOLUME rejection at best, or closes the wrong
        amount at worst.

        **Three answers, not two.** ``None`` means the terminal answered and the position is
        not there; ``UNREADABLE`` means it would not answer. The executor reports "not there"
        as a success, so collapsing the two would turn a momentary read failure into a close
        that never happened or a stop that never moved -- recorded as done either way. An
        empty sequence is MT5's unambiguous "nothing matched"; a bare ``None`` is its
        overloaded one, and is read here as unknown.
        """
        try:
            found = self._mt5.positions_get(ticket=ticket)
        except Exception as exc:
            log.debug("positions_get(ticket=%r) raised: %r", ticket, exc)
            return UNREADABLE
        if found is None:
            return UNREADABLE
        if len(found) == 0:
            return None
        p = found[0]
        try:
            return str(p.symbol), PositionType(int(p.type)), float(p.volume)
        except (AttributeError, TypeError, ValueError) as exc:
            # Unparseable is not absent, for the same reason it is not in _read_positions.
            log.warning("unusable position %r for ticket %s: %s", p, ticket, exc)
            return UNREADABLE

    def order_exists(self, ticket: int) -> bool:
        """Whether a pending order is still resting.

        A read failure answers **True**, which sends a cancel or a modify that may be
        redundant -- both are no-ops against an order that has gone. The alternative is far
        worse: answering False reports "already gone", and every caller treats that as the
        desired state already being true, so a live resting order would be dropped from
        belief and left untracked.

        That means a bare ``None`` has to answer True as well, not just an exception. MT5
        overloads ``None`` for "the call failed" and "nothing matched" -- the same trap
        :meth:`_empty_or_unknown` exists for -- and ``bool(None)`` quietly picks the wrong
        one. Only an empty sequence is an unambiguous "not there".
        """
        try:
            found = self._mt5.orders_get(ticket=ticket)
        except Exception as exc:
            log.debug("orders_get(ticket=%r) raised: %r", ticket, exc)
            return True
        if found is None:
            log.debug("orders_get(ticket=%r) gave None; assuming it is still resting", ticket)
            return True
        return len(found) > 0

    def order_send(self, request: dict[str, Any]) -> Any:
        """The one call in this project that creates an order. Returns the raw MT5 result, or
        None if the call itself failed -- which the executor classifies as a connection
        problem rather than a rejection."""
        try:
            return self._mt5.order_send(request)
        except Exception as exc:
            log.warning("order_send raised: %r", exc)
            return None

    def poll(self, terminal_id: str, seq: int, attempts: int) -> Snapshot:
        """Read account and terminal state into a snapshot.

        A read failure marks the session as not connected so the caller reconnects on the next
        pass, but never calls ``shutdown()`` here -- teardown is the reconnect path's job.
        """
        try:
            account = self._mt5.account_info()
            terminal = self._mt5.terminal_info()
        except Exception as exc:
            self.connected = False
            return Snapshot(
                terminal_id=terminal_id,
                state=WorkerState.DISCONNECTED,
                seq=seq,
                error=f"read failed: {exc!r}",
                connect_attempts=attempts,
            )

        if account is None or terminal is None:
            self.connected = False
            return Snapshot(
                terminal_id=terminal_id,
                state=WorkerState.DISCONNECTED,
                seq=seq,
                error=f"account/terminal info unavailable: {self._last_error()}",
                connect_attempts=attempts,
            )

        open_positions = self._read_positions()
        pending_orders = self._read_orders()

        # A terminal can be reachable while its broker link is down; report that distinctly
        # rather than as a hard failure, because the process is healthy and will recover.
        broker_up = bool(getattr(terminal, "connected", True))

        return Snapshot(
            terminal_id=terminal_id,
            state=WorkerState.CONNECTED if broker_up else WorkerState.DISCONNECTED,
            seq=seq,
            login=getattr(account, "login", None),
            server=getattr(account, "server", None),
            company=getattr(account, "company", None),
            currency=getattr(account, "currency", None),
            balance=getattr(account, "balance", None),
            equity=getattr(account, "equity", None),
            margin_free=getattr(account, "margin_free", None),
            leverage=getattr(account, "leverage", None),
            trade_allowed=getattr(terminal, "trade_allowed", None),
            terminal_connected=broker_up,
            terminal_build=getattr(terminal, "build", None),
            positions=None if open_positions is None else len(open_positions),
            open_positions=open_positions,
            pending_orders=pending_orders,
            error=None if broker_up else "terminal not connected to broker",
            connect_attempts=attempts,
        )

    def _read_orders(self) -> tuple[PendingOrder, ...] | None:
        """Read the resting orders, or None if they could not be read.

        Same discipline as :meth:`_read_positions`, and for a sharper reason: an order missing
        from this list is read as a cancellation, so a failed read misreported as an empty list
        would propose cancelling every copied order at once.
        """
        try:
            raw = self._mt5.orders_get()
        except Exception as exc:
            log.debug("orders_get() raised: %r", exc)
            return None
        if raw is None:
            return self._empty_or_unknown("orders", "orders_total")

        out: list[PendingOrder] = []
        for o in raw:
            try:
                out.append(
                    PendingOrder(
                        ticket=int(o.ticket),
                        symbol=str(o.symbol),
                        type=OrderType(int(o.type)),
                        volume=float(getattr(o, "volume_current", 0.0) or 0.0),
                        price_open=float(o.price_open),
                        sl=float(getattr(o, "sl", 0.0) or 0.0),
                        tp=float(getattr(o, "tp", 0.0) or 0.0),
                        price_stoplimit=float(getattr(o, "price_stoplimit", 0.0) or 0.0),
                        time_setup=int(getattr(o, "time_setup", 0) or 0),
                        magic=int(getattr(o, "magic", 0) or 0),
                        comment=str(getattr(o, "comment", "") or ""),
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                log.warning("unparseable order %r (%s); treating read as failed", o, exc)
                return None
        return tuple(out)

    def _read_positions(self) -> tuple[Position, ...] | None:
        """Read the open positions, or None if they could not be read.

        Replaces the ``positions_total()`` count rather than adding to it: the list already
        carries the count, and one IPC round-trip per tick is cheaper than two.

        Returning None on failure -- and never an empty tuple -- is deliberate. The reconciler
        reads an empty list as a flat master and would propose closing every copy, so a read
        error must be indistinguishable from "do not reconcile", not from "nothing open".
        """
        try:
            raw = self._mt5.positions_get()
        except Exception as exc:
            log.debug("positions_get() raised: %r", exc)
            return None
        if raw is None:
            return self._empty_or_unknown("positions", "positions_total")

        out: list[Position] = []
        for p in raw:
            try:
                out.append(
                    Position(
                        ticket=int(p.ticket),
                        symbol=str(p.symbol),
                        type=PositionType(int(p.type)),
                        volume=float(p.volume),
                        price_open=float(p.price_open),
                        sl=float(getattr(p, "sl", 0.0) or 0.0),
                        tp=float(getattr(p, "tp", 0.0) or 0.0),
                        time=int(getattr(p, "time", 0) or 0),
                        magic=int(getattr(p, "magic", 0) or 0),
                        comment=str(getattr(p, "comment", "") or ""),
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                # A position we cannot parse must not silently vanish from the list, because
                # absence from the master's list is what a CLOSE is derived from. Fail the whole
                # read instead, so this tick is skipped rather than misread.
                log.warning("unparseable position %r (%s); treating read as failed", p, exc)
                return None
        return tuple(out)


def _backoff_delay(schedule: list[float], attempt: int) -> float:
    """Delay before retry ``attempt`` (1-based); the final entry repeats indefinitely."""
    if not schedule:
        return 5.0
    return schedule[min(attempt, len(schedule)) - 1]


def run_worker(
    terminal_id: str,
    exe_path: str,
    out_q: Queue,
    cmd_q: Queue,
    log_file: str,
    poll_interval_ms: int = 250,
    reconnect_backoff_s: list[float] | None = None,
    _session_factory: Callable[[Path], _Session] | None = None,
) -> None:
    """Worker process entry point.

    Must stay a module-level function: Windows uses the *spawn* start method, so the target is
    re-imported and looked up by qualified name in the child. A closure or bound method would
    fail to pickle.

    Publishes a :class:`Snapshot` to ``out_q`` every ``poll_interval_ms``, including while
    disconnected, so the supervisor's heartbeat check can distinguish a wedged worker from an
    unreachable broker. Drains ``cmd_q`` between ticks.

    ``_session_factory`` is a test seam; production passes nothing and the real MT5 module is
    imported here.
    """
    configure(Path(log_file))
    wlog = get_logger(f"worker.{terminal_id[:8]}")
    backoff = reconnect_backoff_s or [1.0, 2.0, 5.0, 10.0, 30.0]
    interval = max(poll_interval_ms, 50) / 1000.0

    if _session_factory is None:
        import MetaTrader5 as mt5  # noqa: N813 -- the package's own capitalisation

        def _session_factory(path: Path) -> _Session:
            return _Session(mt5, path)

    session = _session_factory(Path(exe_path))
    executor = Executor(terminal_id, session)
    seq = 0
    attempts = 0
    next_retry_at = 0.0
    running = True
    close_terminal = False
    """Set from a STOP command's own flag. Stays False for every other way this loop can end
    (a crash, the queue breaking, an unflagged STOP) -- an unplanned exit must never surprise
    the user by closing a terminal they are still looking at."""

    def publish(snap: Snapshot) -> None:
        try:
            out_q.put_nowait(snap)
        except Exception as exc:
            # A full queue means the supervisor is not draining; dropping a snapshot is
            # correct, since the next tick supersedes it anyway.
            wlog.debug("could not publish snapshot: %r", exc)

    def publish_result(result: OrderResult) -> None:
        """Order results are **not** droppable the way snapshots are.

        A snapshot supersedes itself next tick; a result is the only record that an order was
        attempted. Losing one leaves the supervisor believing a trade is still pending that
        has in fact already been placed, so this blocks briefly rather than discarding.
        """
        try:
            out_q.put(result, timeout=2.0)
        except Exception as exc:
            wlog.error("could not report order result for #%s: %r", result.master_ticket, exc)

    wlog.info("worker starting for %s", exe_path)
    publish(Snapshot(terminal_id=terminal_id, state=WorkerState.STARTING, seq=seq))

    try:
        while running:
            # --- commands ------------------------------------------------------------------
            while True:
                try:
                    cmd = cmd_q.get_nowait()
                except queue.Empty:
                    break
                except (EOFError, OSError):
                    running = False
                    break
                if cmd is None or getattr(cmd, "kind", None) is CommandKind.STOP:
                    close_terminal = bool(getattr(cmd, "close_terminal", False))
                    wlog.info("stop requested (close_terminal=%s)", close_terminal)
                    running = False
                    break
                if isinstance(cmd, OrderCommand):
                    if not session.connected:
                        # Refuse rather than queue: by the time the link returns, the master's
                        # trade is old news and the copy would be a late join. Transient, so
                        # the ledger may derive it again -- which it will for a close or a
                        # modify, whose target state does not go stale, and never for an open.
                        publish_result(
                            OrderResult(
                                command_id=cmd.command_id,
                                terminal_id=terminal_id,
                                op=cmd.op,
                                master_ticket=cmd.master_ticket,
                                ok=False,
                                retryable=True,
                                error="terminal not connected when the command arrived",
                            )
                        )
                        continue
                    result = executor.execute(cmd)
                    wlog.info("%s", result.describe())
                    publish_result(result)
                    continue
                if isinstance(cmd, Command) and cmd.kind is CommandKind.RECONNECT:
                    wlog.info("reconnect requested")
                    session.disconnect()
                    # The broker's symbol list is the one input a cached resolution cannot
                    # notice changing, so a reconnect is the deliberate moment to re-read it.
                    executor.forget_symbols()
                    attempts = 0
                    next_retry_at = 0.0
            if not running:
                break

            # --- connection ----------------------------------------------------------------
            if not session.connected:
                now = time.monotonic()
                if now < next_retry_at:
                    seq += 1
                    publish(
                        Snapshot(
                            terminal_id=terminal_id,
                            state=WorkerState.CONNECTING,
                            seq=seq,
                            error=f"retrying in {next_retry_at - now:.0f}s",
                            connect_attempts=attempts,
                        )
                    )
                    time.sleep(min(interval, max(next_retry_at - now, 0.0)))
                    continue

                attempts += 1
                seq += 1
                publish(
                    Snapshot(
                        terminal_id=terminal_id,
                        state=WorkerState.CONNECTING,
                        seq=seq,
                        connect_attempts=attempts,
                    )
                )
                ok, err = session.connect()
                if not ok:
                    delay = _backoff_delay(backoff, attempts)
                    next_retry_at = time.monotonic() + delay
                    wlog.warning("connect attempt %d failed: %s", attempts, err)
                    seq += 1
                    publish(
                        Snapshot(
                            terminal_id=terminal_id,
                            state=WorkerState.FAILED,
                            seq=seq,
                            error=err,
                            connect_attempts=attempts,
                        )
                    )
                    continue
                wlog.info("connected after %d attempt(s)", attempts)
                attempts = 0

            # --- poll ----------------------------------------------------------------------
            seq += 1
            snap = session.poll(terminal_id, seq, attempts)
            publish(snap)
            if not session.connected:
                next_retry_at = time.monotonic() + _backoff_delay(backoff, 1)
            time.sleep(interval)

    except KeyboardInterrupt:
        wlog.info("interrupted")
    except Exception:
        wlog.exception("worker crashed")
        seq += 1
        publish(
            Snapshot(
                terminal_id=terminal_id,
                state=WorkerState.FAILED,
                seq=seq,
                error="worker crashed; see log",
            )
        )
    finally:
        session.disconnect(close_terminal=close_terminal)
        seq += 1
        publish(Snapshot(terminal_id=terminal_id, state=WorkerState.STOPPED, seq=seq))
        wlog.info("worker stopped")
