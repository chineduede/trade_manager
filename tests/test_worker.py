"""Worker tests, driven by a fake MT5 module.

No real terminal is touched. The fake reproduces the shapes the real extension returns
(namedtuple-ish objects, ``None`` on failure, ``last_error()`` tuples) and lets each call be
made to fail on demand, which is the only way to test the reconnect paths deterministically.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from mt5copier.protocol import (
    Command,
    CommandKind,
    OrderType,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.worker import _backoff_delay, _Session, run_worker


@dataclass
class FakeAccount:
    login: int = 1514431066
    server: str = "FTMO-Demo"
    company: str = "FTMO Global Markets"
    currency: str = "USD"
    balance: float = 99_880.0
    equity: float = 99_912.5
    margin_free: float = 99_000.0
    leverage: int = 100


@dataclass
class FakeTerminal:
    connected: bool = True
    trade_allowed: bool = True
    build: int = 4885


@dataclass
class FakePosition:
    """Shaped like what ``positions_get()`` returns -- attribute access, MT5's int enums."""

    ticket: int = 100
    symbol: str = "EURUSD"
    type: int = 0
    volume: float = 0.5
    price_open: float = 1.08500
    sl: float = 1.08000
    tp: float = 0.0
    time: int = 1_700_000_000
    magic: int = 0
    comment: str = ""


@dataclass
class FakeOrder:
    """Shaped like what ``orders_get()`` returns, including ``volume_current``."""

    ticket: int = 200
    symbol: str = "EURUSD"
    type: int = 2  # ORDER_TYPE_BUY_LIMIT
    volume_current: float = 0.4
    price_open: float = 1.08000
    sl: float = 1.07500
    tp: float = 0.0
    price_stoplimit: float = 0.0
    time_setup: int = 1_700_000_000
    magic: int = 0
    comment: str = ""


def fake_positions(count: int = 3) -> tuple[FakePosition, ...]:
    return tuple(FakePosition(ticket=100 + i) for i in range(count))


class FakeMT5:
    """Stand-in for the MetaTrader5 extension module."""

    def __init__(
        self,
        *,
        init_ok: bool = True,
        account: FakeAccount | None = None,
        terminal: FakeTerminal | None = None,
        raise_on_read: bool = False,
        positions: tuple[FakePosition, ...] | None = None,
        orders: tuple[FakeOrder, ...] = (),
    ) -> None:
        self.init_ok = init_ok
        self._account = account if account is not None else FakeAccount()
        self._terminal = terminal if terminal is not None else FakeTerminal()
        self.raise_on_read = raise_on_read
        self.positions = fake_positions() if positions is None else positions
        self.orders: tuple[FakeOrder, ...] | None = orders
        self.init_calls: list[str] = []
        self.shutdown_calls = 0

    def initialize(self, path: str, timeout: int = 0, **kw: object) -> bool:
        self.init_calls.append(path)
        return self.init_ok

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def last_error(self) -> tuple[int, str]:
        return (-10005, "IPC timeout")

    def account_info(self) -> FakeAccount | None:
        if self.raise_on_read:
            raise RuntimeError("boom")
        return self._account

    def terminal_info(self) -> FakeTerminal | None:
        if self.raise_on_read:
            raise RuntimeError("boom")
        return self._terminal

    def positions_get(self) -> tuple[FakePosition, ...] | None:
        if self.positions is None:
            return None
        return self.positions

    def orders_get(self) -> tuple[FakeOrder, ...] | None:
        if self.orders is None:
            return None
        return self.orders


# --- backoff -------------------------------------------------------------------------------


def test_backoff_follows_schedule_then_repeats_last() -> None:
    schedule = [1.0, 2.0, 5.0]
    assert [_backoff_delay(schedule, n) for n in (1, 2, 3, 4, 99)] == [1.0, 2.0, 5.0, 5.0, 5.0]


def test_backoff_with_empty_schedule_has_a_sane_default() -> None:
    assert _backoff_delay([], 1) == 5.0


# --- session -------------------------------------------------------------------------------


def test_connect_passes_exe_path_to_initialize() -> None:
    mt5 = FakeMT5()
    session = _Session(mt5, Path("C:/pf/Broker/terminal64.exe"))
    ok, err = session.connect()
    assert ok and err is None and session.connected
    assert mt5.init_calls == ["C:\\pf\\Broker\\terminal64.exe"]


def test_connect_failure_reports_last_error() -> None:
    session = _Session(FakeMT5(init_ok=False), Path("x.exe"))
    ok, err = session.connect()
    assert not ok and not session.connected
    assert err is not None and "IPC timeout" in err


def test_connect_survives_initialize_raising() -> None:
    class Exploding(FakeMT5):
        def initialize(self, path: str, timeout: int = 0, **kw: object) -> bool:
            raise OSError("access denied")

    ok, err = _Session(Exploding(), Path("x.exe")).connect()
    assert not ok and err is not None and "access denied" in err


def test_poll_returns_full_account_snapshot() -> None:
    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.state is WorkerState.CONNECTED and snap.is_live
    assert snap.login == 1514431066
    assert snap.equity == 99_912.5
    assert snap.currency == "USD"
    assert snap.positions == 3
    assert snap.trade_allowed is True
    assert snap.error is None


def test_poll_converts_open_positions_for_the_diff_engine() -> None:
    session = _Session(
        FakeMT5(positions=(FakePosition(ticket=77, type=1, volume=0.25),)), Path("x")
    )
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.open_positions is not None
    (p,) = snap.open_positions
    assert p.ticket == 77
    assert p.type is PositionType.SELL
    assert p.volume == 0.25
    assert p.has_stop
    assert snap.positions == 1, "the count comes from the list, not a second IPC call"


def test_a_flat_account_is_an_empty_tuple_not_none() -> None:
    session = _Session(FakeMT5(positions=()), Path("x.exe"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.open_positions == ()
    assert snap.positions == 0


def test_a_failed_position_read_is_unknown_not_flat() -> None:
    """None and () must stay distinguishable: the reconciler derives a CLOSE for every copy
    missing from the master's list, so a failed read read as "flat" would close everything."""
    mt5 = FakeMT5()
    mt5.positions = None
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.open_positions is None
    assert snap.positions is None


def test_positions_get_raising_is_an_unknown_read() -> None:
    class Exploding(FakeMT5):
        def positions_get(self) -> tuple[FakePosition, ...] | None:
            raise RuntimeError("ipc timeout")

    session = _Session(Exploding(), Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).open_positions is None


def test_one_unparseable_position_fails_the_whole_read() -> None:
    """Dropping it from the list would look exactly like the master having closed it."""

    @dataclass
    class Broken:
        ticket: str = "not-a-ticket"

    session = _Session(FakeMT5(positions=(Broken(),)), Path("x.exe"))  # type: ignore[arg-type]
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).open_positions is None


def test_poll_converts_resting_orders() -> None:
    session = _Session(
        FakeMT5(orders=(FakeOrder(ticket=200, type=5, volume_current=0.4),)), Path("x")
    )
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.pending_orders is not None
    (o,) = snap.pending_orders
    assert o.ticket == 200
    assert o.type is OrderType.SELL_STOP
    assert o.volume == 0.4, "volume_current, i.e. what is still unfilled"


def test_no_resting_orders_is_an_empty_tuple_not_none() -> None:
    session = _Session(FakeMT5(orders=()), Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).pending_orders == ()


def test_a_failed_order_read_is_unknown_not_empty() -> None:
    """A vanished order reads as a cancellation, so an empty list after a failed read would
    propose cancelling every copied order at once."""
    mt5 = FakeMT5()
    mt5.orders = None
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).pending_orders is None


def test_one_unparseable_order_fails_the_whole_read() -> None:
    @dataclass
    class Broken:
        ticket: str = "nope"

    session = _Session(FakeMT5(orders=(Broken(),)), Path("x.exe"))  # type: ignore[arg-type]
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).pending_orders is None


def test_poll_reports_broker_link_down_as_disconnected_not_failed() -> None:
    """A terminal that is up but has lost its broker is recoverable; it must not read as a
    hard failure, or the UI would imply the worker needs restarting."""
    session = _Session(FakeMT5(terminal=FakeTerminal(connected=False)), Path("x.exe"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.state is WorkerState.DISCONNECTED
    assert snap.terminal_connected is False
    assert snap.error is not None


def test_failed_read_does_not_shut_down_the_session() -> None:
    """Regression guard for the zele-mt5 bug where get_acct_info() called mt5.shutdown() on a
    read failure, destroying the process's connection as a side effect of a query."""
    mt5 = FakeMT5(raise_on_read=True)
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.state is WorkerState.DISCONNECTED
    assert not session.connected  # marked for reconnect
    assert mt5.shutdown_calls == 0  # but NOT torn down here


def test_poll_handles_none_account_info() -> None:
    class NoAccount(FakeMT5):
        def account_info(self) -> FakeAccount | None:
            return None

    mt5 = NoAccount()
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.state is WorkerState.DISCONNECTED
    assert mt5.shutdown_calls == 0


def test_disconnect_is_idempotent() -> None:
    mt5 = FakeMT5()
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    session.disconnect()
    session.disconnect()
    assert mt5.shutdown_calls == 1


# --- full worker loop ----------------------------------------------------------------------


def drain(q: queue.Queue) -> list[Snapshot]:
    out: list[Snapshot] = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def run_until_stopped(
    mt5: FakeMT5, tmp_path: Path, *, timeout: float = 5.0, poll_ms: int = 50
) -> list[Snapshot]:
    """Run the worker in a thread, wait for it to connect, then stop it cleanly."""
    out_q: queue.Queue = queue.Queue()
    cmd_q: queue.Queue = queue.Queue()

    thread = threading.Thread(
        target=run_worker,
        args=("TID12345", "x.exe", out_q, cmd_q, str(tmp_path / "w.log"), poll_ms, [0.01]),
        kwargs={"_session_factory": lambda path: _Session(mt5, path)},
        daemon=True,
    )
    thread.start()

    deadline = threading.Event()
    collected: list[Snapshot] = []
    for _ in range(int(timeout / 0.05)):
        collected += drain(out_q)
        if any(s.state is WorkerState.CONNECTED for s in collected):
            break
        if any(s.state is WorkerState.FAILED for s in collected):
            break
        deadline.wait(0.05)

    cmd_q.put(Command(kind=CommandKind.STOP))
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "worker did not stop"
    return collected + drain(out_q)


def test_worker_lifecycle_emits_starting_connected_stopped(tmp_path: Path) -> None:
    snaps = run_until_stopped(FakeMT5(), tmp_path)
    states = [s.state for s in snaps]
    assert states[0] is WorkerState.STARTING
    assert WorkerState.CONNECTED in states
    assert states[-1] is WorkerState.STOPPED
    assert all(s.terminal_id == "TID12345" for s in snaps)


def test_worker_sequence_numbers_increase(tmp_path: Path) -> None:
    """The supervisor drops out-of-order snapshots by seq, so these must be monotonic."""
    seqs = [s.seq for s in run_until_stopped(FakeMT5(), tmp_path)]
    assert seqs == sorted(seqs)


def test_worker_keeps_reporting_while_failing_to_connect(tmp_path: Path) -> None:
    """A terminal we cannot reach must still produce snapshots, otherwise the supervisor
    cannot tell 'unreachable' from 'our worker wedged'."""
    snaps = run_until_stopped(FakeMT5(init_ok=False), tmp_path, timeout=3.0)
    states = {s.state for s in snaps}
    assert WorkerState.CONNECTING in states
    assert WorkerState.FAILED in states
    assert snaps[-1].state is WorkerState.STOPPED
    failed = [s for s in snaps if s.state is WorkerState.FAILED]
    assert failed[0].connect_attempts >= 1
    assert failed[0].error is not None


def test_worker_shuts_down_session_on_exit(tmp_path: Path) -> None:
    mt5 = FakeMT5()
    run_until_stopped(mt5, tmp_path)
    assert mt5.shutdown_calls >= 1


def test_worker_stops_on_bare_none_sentinel(tmp_path: Path) -> None:
    """zele-mt5's PluginManager used a bare None as its stop sentinel; accept that too."""
    out_q: queue.Queue = queue.Queue()
    cmd_q: queue.Queue = queue.Queue()
    mt5 = FakeMT5()
    thread = threading.Thread(
        target=run_worker,
        args=("TID", "x.exe", out_q, cmd_q, str(tmp_path / "w.log"), 50, [0.01]),
        kwargs={"_session_factory": lambda path: _Session(mt5, path)},
        daemon=True,
    )
    thread.start()
    cmd_q.put(None)
    thread.join(timeout=5.0)
    assert not thread.is_alive()


@pytest.mark.parametrize("poll_ms", [50, 1])
def test_worker_clamps_poll_interval(tmp_path: Path, poll_ms: int) -> None:
    """A tiny poll interval must not become a busy-spin against the terminal's IPC."""
    snaps = run_until_stopped(FakeMT5(), tmp_path, poll_ms=poll_ms)
    assert any(s.state is WorkerState.CONNECTED for s in snaps)
