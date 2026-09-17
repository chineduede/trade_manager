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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logging_setup import configure, get_logger
from .protocol import (
    Command,
    CommandKind,
    OrderType,
    PendingOrder,
    Position,
    PositionType,
    Snapshot,
    WorkerState,
)

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

    def connect(self) -> tuple[bool, str | None]:
        """Call ``initialize(path=...)``. Returns (ok, error message).

        This is the only place that may launch a terminal: per the MT5 docs, ``initialize()``
        starts the terminal if it is not already running. That is why discovery never calls it
        and why this runs only for terminals the user has explicitly designated.
        """
        try:
            ok = self._mt5.initialize(str(self._exe_path), timeout=_INIT_TIMEOUT_MS)
        except Exception as exc:  # the extension can raise, not just return False
            return False, f"initialize() raised: {exc!r}"
        if not ok:
            return False, f"initialize() failed: {self._last_error()}"
        self.connected = True
        return True, None

    def disconnect(self) -> None:
        if not self.connected:
            return
        try:
            self._mt5.shutdown()
        except Exception as exc:
            log.debug("shutdown() raised, ignoring: %r", exc)
        finally:
            self.connected = False

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
    _session_factory: Any = None,
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

        def _session_factory(path: Path) -> _Session:  # type: ignore[misc]
            return _Session(mt5, path)

    session = _session_factory(Path(exe_path))
    seq = 0
    attempts = 0
    next_retry_at = 0.0
    running = True

    def publish(snap: Snapshot) -> None:
        try:
            out_q.put_nowait(snap)
        except Exception as exc:
            # A full queue means the supervisor is not draining; dropping a snapshot is
            # correct, since the next tick supersedes it anyway.
            wlog.debug("could not publish snapshot: %r", exc)

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
                    wlog.info("stop requested")
                    running = False
                    break
                if isinstance(cmd, Command) and cmd.kind is CommandKind.RECONNECT:
                    wlog.info("reconnect requested")
                    session.disconnect()
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
        session.disconnect()
        seq += 1
        publish(Snapshot(terminal_id=terminal_id, state=WorkerState.STOPPED, seq=seq))
        wlog.info("worker stopped")
