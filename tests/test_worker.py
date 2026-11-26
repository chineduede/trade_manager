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

from mt5copier.execution import backoff_delay
from mt5copier.executor import UNREADABLE
from mt5copier.protocol import (
    Command,
    CommandKind,
    OrderType,
    PositionType,
    RatesRequest,
    RatesResult,
    Snapshot,
    WarmCommand,
    WatchCommand,
    WorkerState,
)
from mt5copier.worker import _expiration_utc, _Session, run_worker


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
    margin: float = 912.5
    margin_level: float = 10_949.3
    profit: float = 32.5


@dataclass
class FakeTerminal:
    connected: bool = True
    trade_allowed: bool = True
    build: int = 4885
    ping_last: int = 41_250


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
    price_current: float = 1.08620
    profit: float = 60.0
    swap: float = -1.25


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
    type_time: int = 0  # ORDER_TIME_GTC
    time_expiration: int = 0


def fake_positions(count: int = 3) -> tuple[FakePosition, ...]:
    return tuple(FakePosition(ticket=100 + i) for i in range(count))


@dataclass
class FakeSymbolInfo:
    """Shaped like what ``symbol_info()`` returns for a symbol the broker actually lists."""

    name: str = "EURUSD"
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    point: float = 0.0001
    digits: int = 5
    filling_mode: int = 1
    visible: bool = True
    trade_tick_size: float = 0.00001
    trade_tick_value_loss: float = 1.0


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
        symbol_info_data: dict[str, FakeSymbolInfo] | None = None,
        select_fails_for: frozenset[str] = frozenset(),
    ) -> None:
        self.init_ok = init_ok
        self._account = account if account is not None else FakeAccount()
        self._terminal = terminal if terminal is not None else FakeTerminal()
        self.raise_on_read = raise_on_read
        self.positions = fake_positions() if positions is None else positions
        self.orders: tuple[FakeOrder, ...] | None = orders
        self.positions_total_value: int | None = None
        self.orders_total_value: int | None = None
        self.init_calls: list[str] = []
        self.shutdown_calls = 0
        self._symbol_info = symbol_info_data or {}
        # Names on which symbol_select() itself reports failure, the way a terminal does when
        # it cannot act on the request immediately rather than the symbol not existing.
        self._select_fails_for = select_fails_for
        self.symbol_select_calls: list[str] = []
        self.symbol_info_calls: list[str] = []

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

    def positions_get(self, ticket: int | None = None) -> tuple[FakePosition, ...] | None:
        if self.positions is None:
            return None
        if ticket is None:
            return self.positions
        return tuple(p for p in self.positions if p.ticket == ticket)

    def orders_get(self, ticket: int | None = None) -> tuple[FakeOrder, ...] | None:
        if self.orders is None:
            return None
        if ticket is None:
            return self.orders
        return tuple(o for o in self.orders if o.ticket == ticket)

    # MT5 returns None from the *_get calls both for "nothing found" and for a failure, so the
    # session cross-checks these counts to tell the two apart. The overrides let a test say
    # "the get call returned None *and* there really are three open", i.e. a genuine failure.
    def positions_total(self) -> int:
        if self.positions_total_value is not None:
            return self.positions_total_value
        return len(self.positions or ())

    def orders_total(self) -> int:
        if self.orders_total_value is not None:
            return self.orders_total_value
        return len(self.orders or ())

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        self.symbol_select_calls.append(name)
        info = self._symbol_info.get(name)
        if info is None or name in self._select_fails_for:
            return False
        info.visible = enable
        return True

    def symbol_info(self, name: str) -> FakeSymbolInfo | None:
        self.symbol_info_calls.append(name)
        return self._symbol_info.get(name)


# --- backoff -------------------------------------------------------------------------------


def test_backoff_follows_schedule_then_repeats_last() -> None:
    schedule = [1.0, 2.0, 5.0]
    assert [backoff_delay(schedule, n) for n in (1, 2, 3, 4, 99)] == [1.0, 2.0, 5.0, 5.0, 5.0]


def test_backoff_with_empty_schedule_has_a_sane_default() -> None:
    assert backoff_delay([], 1) == 5.0


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


def test_the_symbol_catalog_rides_the_first_snapshot_of_each_connection() -> None:
    class WithCatalog(FakeMT5):
        def symbols_get(self) -> tuple[FakeSymbolInfo, ...]:
            return (FakeSymbolInfo(name="EURUSD.r"), FakeSymbolInfo(name="XAUUSD.r"))

    session = _Session(WithCatalog(), Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).symbols == ("EURUSD.r", "XAUUSD.r")
    # Once per connection, not per poll: the list is big and does not change mid-session.
    assert session.poll("TID", seq=2, attempts=0).symbols is None
    # A reconnect pushes afresh -- the broker's list may differ across it.
    session.connect()
    assert session.poll("TID", seq=3, attempts=0).symbols == ("EURUSD.r", "XAUUSD.r")


def test_a_broker_that_answers_no_catalog_is_asked_again_next_poll() -> None:
    session = _Session(FakeMT5(), Path("x.exe"))  # FakeMT5 has no symbols_get at all
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).symbols is None
    assert session.poll("TID", seq=2, attempts=0).symbols is None


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
    mt5.positions_total_value = 3  # the get call failed; three really are open
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.open_positions is None
    assert snap.positions is None


def test_a_none_position_read_with_a_zero_count_really_is_flat() -> None:
    """MT5 returns None for "nothing found" as well as for failure. Reading the first as
    unknown would freeze the copier the moment the master went flat."""
    mt5 = FakeMT5()
    mt5.positions = None
    mt5.positions_total_value = 0
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).open_positions == ()


def test_a_missing_count_function_leaves_the_read_unknown() -> None:
    """And must not raise out of poll, which would take the worker down."""

    class NoTotals(FakeMT5):
        positions_total = None  # type: ignore[assignment]

    mt5 = NoTotals()
    mt5.positions = None
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).open_positions is None


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


def test_poll_carries_live_values_beside_each_position() -> None:
    session = _Session(FakeMT5(positions=(FakePosition(ticket=77),)), Path("x"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.live_positions is not None
    (live,) = snap.live_positions
    assert (live.ticket, live.price_current, live.profit, live.swap) == (77, 1.08620, 60.0, -1.25)


def test_a_moving_pl_leaves_the_position_itself_unchanged() -> None:
    """The whole point of keeping live values apart: the reconciler compares positions, and a
    P/L tick must not look like a modification."""
    mt5 = FakeMT5(positions=(FakePosition(ticket=77),))
    session = _Session(mt5, Path("x"))
    session.connect()
    first = session.poll("TID", seq=1, attempts=0)
    mt5.positions = (FakePosition(ticket=77, price_current=1.0900, profit=150.0, swap=-2.0),)
    second = session.poll("TID", seq=2, attempts=0)
    assert first.open_positions == second.open_positions
    assert first.live_positions != second.live_positions


def test_live_values_are_unknown_exactly_when_positions_are() -> None:
    mt5 = FakeMT5()
    mt5.positions = None
    mt5.positions_total_value = 3
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).live_positions is None
    mt5.positions_total_value = 0
    assert session.poll("TID", seq=2, attempts=0).live_positions == ()


def test_a_missing_or_junk_live_value_is_blank_not_a_failed_read() -> None:
    @dataclass
    class Sparse(FakePosition):
        profit: object = None
        swap: object = "n/a"

    session = _Session(FakeMT5(positions=(Sparse(ticket=5),)), Path("x"))  # type: ignore[arg-type]
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.open_positions is not None and len(snap.open_positions) == 1
    assert snap.live_positions is not None
    (live,) = snap.live_positions
    assert live.profit is None and live.swap is None and live.price_current == 1.08620


def test_poll_reads_margin_floating_pl_and_ping() -> None:
    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert (snap.margin, snap.margin_level, snap.profit) == (912.5, 10_949.3, 32.5)
    assert snap.ping_last_us == 41_250


def test_a_broker_without_the_display_fields_still_connects() -> None:
    @dataclass
    class BareAccount:
        login: int = 1
        balance: float = 100.0
        equity: float = 100.0

    @dataclass
    class BareTerminal:
        connected: bool = True

    session = _Session(
        FakeMT5(account=BareAccount(), terminal=BareTerminal()),  # type: ignore[arg-type]
        Path("x.exe"),
    )
    session.connect()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.is_live
    assert snap.margin is None and snap.margin_level is None and snap.profit is None
    assert snap.ping_last_us is None


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


def test_an_orders_expiry_is_put_on_the_utc_line_with_the_brokers_offset() -> None:
    """MT5 stamps the expiry in the broker's own clock; two hours ahead means two hours
    subtracted to reach real UTC."""
    timed = FakeOrder(type_time=2, time_expiration=1_780_007_200)  # ORDER_TIME_SPECIFIED
    assert _expiration_utc(timed, 7_200) == 1_780_000_000


def test_an_expiry_without_a_known_offset_reads_as_none() -> None:
    """Guessing could be hours off; the tick that finds the offset reports it properly."""
    timed = FakeOrder(type_time=2, time_expiration=1_780_007_200)
    assert _expiration_utc(timed, None) == 0


def test_only_a_specified_expiry_is_copyable() -> None:
    """GTC has no timestamp and a DAY order's day is the broker's, not a moment in time."""
    assert _expiration_utc(FakeOrder(), 7_200) == 0
    day = FakeOrder(type_time=1, time_expiration=1_780_007_200)  # ORDER_TIME_DAY
    assert _expiration_utc(day, 7_200) == 0


def test_no_resting_orders_is_an_empty_tuple_not_none() -> None:
    session = _Session(FakeMT5(orders=()), Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).pending_orders == ()


def test_a_failed_order_read_is_unknown_not_empty() -> None:
    """A vanished order reads as a cancellation, so an empty list after a failed read would
    propose cancelling every copied order at once."""
    mt5 = FakeMT5()
    mt5.orders = None
    mt5.orders_total_value = 2
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).pending_orders is None


def test_deleting_the_last_order_reads_as_empty_not_unknown() -> None:
    """The regression behind "deleting a pending order does not reflect": MT5 can answer None
    rather than () once the last order is gone, and an unknown read pauses detection entirely,
    so the cancellation was never derived."""
    mt5 = FakeMT5()
    mt5.orders = None
    mt5.orders_total_value = 0
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    assert session.poll("TID", seq=1, attempts=0).pending_orders == ()


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
    """Regression guard for the classic bug where get_acct_info() called mt5.shutdown() on a
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


# --- launching and closing the terminal we started ------------------------------------------
#
# connect() can launch the terminal (MT5's own initialize() does that); symmetrically, we must
# be the one to close it again -- but only the instance we actually started, never one the user
# already had open.


class FakeProcess:
    """Stand-in for ``psutil.Process``, for the one call ``disconnect()`` makes against it."""

    def __init__(self, pid: int, exe: str = "x.exe") -> None:
        self.pid = pid
        self._exe = exe
        self.terminated = False

    def exe(self) -> str:
        return self._exe

    def terminate(self) -> None:
        self.terminated = True


def _lookup_that_appears_after_launch(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    """Nothing running before connect(), the given pid running after -- i.e. we launched it."""
    calls = {"n": 0}

    def fake_lookup(path: Path) -> int | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else pid

    monkeypatch.setattr("mt5copier.worker.find_terminal_pid", fake_lookup)


def test_connect_remembers_the_pid_it_launched(monkeypatch: pytest.MonkeyPatch) -> None:
    _lookup_that_appears_after_launch(monkeypatch, 4242)
    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    assert session._launched_pid == 4242


def test_connect_does_not_claim_a_terminal_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mt5copier.worker.find_terminal_pid", lambda path: 9999)
    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    assert session._launched_pid is None


def test_disconnect_with_close_terminal_closes_what_it_launched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lookup_that_appears_after_launch(monkeypatch, 4242)
    proc = FakeProcess(4242, exe="x.exe")
    monkeypatch.setattr("mt5copier.worker.psutil.Process", lambda pid: proc)

    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    session.disconnect(close_terminal=True)
    assert proc.terminated


def test_plain_disconnect_never_closes_the_terminal() -> None:
    """The reconnect path calls this. Closing the terminal here would kill the very instance
    it is about to reconnect to."""
    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    session._launched_pid = 4242  # pretend we launched one, to prove the default ignores it
    session.disconnect()
    # No psutil.Process patched in at all; reaching for it would raise, so a clean run proves
    # _close_launched_terminal() was never called.


def test_close_terminal_never_touches_one_we_did_not_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mt5copier.worker.find_terminal_pid", lambda path: 4242)  # already up
    touched: list[int] = []
    monkeypatch.setattr(
        "mt5copier.worker.psutil.Process",
        lambda pid: touched.append(pid) or FakeProcess(pid),
    )
    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    session.disconnect(close_terminal=True)
    assert touched == []


def test_close_terminal_refuses_a_pid_the_os_recycled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pid we recorded now belongs to something else entirely -- never act on it."""
    _lookup_that_appears_after_launch(monkeypatch, 4242)
    proc = FakeProcess(4242, exe="notepad.exe")  # not the terminal any more
    monkeypatch.setattr("mt5copier.worker.psutil.Process", lambda pid: proc)

    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    session.disconnect(close_terminal=True)
    assert not proc.terminated


def test_close_terminal_tolerates_the_process_already_being_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    _lookup_that_appears_after_launch(monkeypatch, 4242)

    def raise_gone(pid: int) -> FakeProcess:
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr("mt5copier.worker.psutil.Process", raise_gone)

    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    session.disconnect(close_terminal=True)  # must not raise


# --- full worker loop ----------------------------------------------------------------------


def drain(q: queue.Queue) -> list[Snapshot]:
    out: list[Snapshot] = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


_PLAIN_STOP = Command(kind=CommandKind.STOP)


def run_until_stopped(
    mt5: FakeMT5,
    tmp_path: Path,
    *,
    timeout: float = 5.0,
    poll_ms: int = 50,
    stop_command: Command = _PLAIN_STOP,
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

    cmd_q.put(stop_command)
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


def test_worker_closes_the_terminal_it_launched_when_told_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _lookup_that_appears_after_launch(monkeypatch, 4242)
    proc = FakeProcess(4242, exe="x.exe")
    monkeypatch.setattr("mt5copier.worker.psutil.Process", lambda pid: proc)

    run_until_stopped(
        FakeMT5(), tmp_path, stop_command=Command(kind=CommandKind.STOP, close_terminal=True)
    )
    assert proc.terminated


def test_worker_leaves_the_terminal_open_on_a_plain_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _lookup_that_appears_after_launch(monkeypatch, 4242)
    proc = FakeProcess(4242, exe="x.exe")
    monkeypatch.setattr("mt5copier.worker.psutil.Process", lambda pid: proc)

    run_until_stopped(FakeMT5(), tmp_path)  # the default stop_command, close_terminal=False
    assert not proc.terminated


def test_worker_stops_on_bare_none_sentinel(tmp_path: Path) -> None:
    """Some supervisors use a bare None as their stop sentinel; accept that too."""
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


# --- the reads the executor acts on -----------------------------------------------------------
#
# These three answers decide whether a copy gets closed, cancelled or left alone, and two of
# them are reported to the engine as *successes*. They had no coverage before Stage 3.


def test_position_reads_back_the_symbol_direction_and_volume() -> None:
    mt5 = FakeMT5(positions=(FakePosition(ticket=100, symbol="EURUSD", type=1, volume=0.25),))
    assert _Session(mt5, Path("x.exe")).position(100) == ("EURUSD", PositionType.SELL, 0.25)


def test_a_position_the_terminal_says_is_not_there_reads_as_gone() -> None:
    """An empty sequence is MT5's unambiguous "nothing matched"."""
    assert _Session(FakeMT5(positions=()), Path("x.exe")).position(999) is None


def test_a_position_read_that_fails_is_unreadable_not_gone() -> None:
    """The distinction the whole three-state answer exists for: "gone" is reported as a
    success, so a failed read must never look like one."""
    mt5 = FakeMT5()
    mt5.positions = None  # the constructor reads None as "use the default three"
    assert _Session(mt5, Path("x.exe")).position(100) is UNREADABLE


def test_a_raising_position_read_is_unreadable_too() -> None:
    class Exploding(FakeMT5):
        def positions_get(self, ticket: int | None = None):
            raise RuntimeError("boom")

    assert _Session(Exploding(), Path("x.exe")).position(100) is UNREADABLE


def test_an_unparseable_position_is_unreadable_rather_than_absent() -> None:
    """Dropping it would be indistinguishable from the position having closed."""
    mt5 = FakeMT5(positions=(FakePosition(ticket=100, type=99),))
    assert _Session(mt5, Path("x.exe")).position(100) is UNREADABLE


def test_a_resting_order_is_reported_as_resting() -> None:
    mt5 = FakeMT5(orders=(FakeOrder(ticket=200),))
    assert _Session(mt5, Path("x.exe")).order_exists(200) is True


def test_an_order_the_terminal_says_is_not_there_reads_as_gone() -> None:
    assert _Session(FakeMT5(orders=()), Path("x.exe")).order_exists(999) is False


def test_an_order_read_that_fails_answers_still_resting() -> None:
    """MT5 overloads None for "call failed" and "nothing matched", and bool(None) picks the
    wrong one. Answering False would drop a live order from belief and leave it untracked;
    answering True at worst sends a cancel or a modify that is a no-op."""
    assert _Session(FakeMT5(orders=None), Path("x.exe")).order_exists(200) is True


def test_a_raising_order_read_answers_still_resting() -> None:
    class Exploding(FakeMT5):
        def orders_get(self, ticket: int | None = None):
            raise RuntimeError("boom")

    assert _Session(Exploding(), Path("x.exe")).order_exists(200) is True


# --- symbol facts ----------------------------------------------------------------------------
#
# ``.visible`` is the signal, not "was symbol_info() None" -- an unselected symbol does not
# reliably answer with None, some brokers hand back a non-None struct with zeroed fields
# instead. Select only when hidden, re-read afterward rather than trusting the stale pre-select
# struct, and if the select call itself fails, one retry after a short wait follows.


def test_symbol_facts_does_not_reselect_an_already_visible_symbol() -> None:
    mt5 = FakeMT5(
        symbol_info_data={"EURUSD": FakeSymbolInfo(name="EURUSD", filling_mode=2, visible=True)}
    )
    facts = _Session(mt5, Path("x.exe")).symbol_facts("EURUSD")
    assert mt5.symbol_select_calls == [], "already visible, so selecting again would be pointless"
    assert mt5.symbol_info_calls == ["EURUSD"]
    assert facts is not None and facts.filling_mask == 2


def test_symbol_facts_selects_and_rereads_a_hidden_symbol() -> None:
    """The struct read before selecting can be the stale, zeroed one -- the fix is reading
    again afterward, not trusting that the first read already reflects the new state."""
    mt5 = FakeMT5(
        symbol_info_data={"XAUUSD": FakeSymbolInfo(name="XAUUSD", filling_mode=4, visible=False)}
    )
    facts = _Session(mt5, Path("x.exe")).symbol_facts("XAUUSD")
    assert mt5.symbol_select_calls == ["XAUUSD"]
    assert mt5.symbol_info_calls == ["XAUUSD", "XAUUSD"], "must re-read after selecting"
    assert facts is not None and facts.filling_mask == 4


def test_symbol_facts_never_selects_a_symbol_the_broker_does_not_list() -> None:
    """Mirrors the reference this was modelled on: a missing symbol_info() answer is checked
    before visibility is even asked about, so a name the broker does not have is never selected."""
    mt5 = FakeMT5(symbol_info_data={})
    facts = _Session(mt5, Path("x.exe")).symbol_facts("NOSUCHSYMBOL")
    assert mt5.symbol_select_calls == []
    assert facts is None


def test_symbol_facts_retries_after_a_sleep_when_select_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal that cannot act on the select instantly is not the same as the symbol not
    existing -- one retry follows a short wait rather than failing outright."""
    sleeps: list[float] = []
    monkeypatch.setattr("mt5copier.worker.time.sleep", sleeps.append)
    mt5 = FakeMT5(
        symbol_info_data={"EURUSD": FakeSymbolInfo(name="EURUSD", visible=False)},
        select_fails_for=frozenset({"EURUSD"}),
    )
    facts = _Session(mt5, Path("x.exe")).symbol_facts("EURUSD")
    assert mt5.symbol_select_calls == ["EURUSD"]
    assert sleeps == [2.0]
    assert mt5.symbol_info_calls == ["EURUSD", "EURUSD"], "one retried read after the sleep"
    assert facts is not None, "a select failure is not the same as the symbol not existing"


def test_a_raising_symbol_select_leaves_facts_unreadable() -> None:
    class Exploding(FakeMT5):
        def symbol_select(self, name: str, enable: bool = True) -> bool:
            raise RuntimeError("boom")

    mt5 = Exploding(symbol_info_data={"EURUSD": FakeSymbolInfo(name="EURUSD", visible=False)})
    assert _Session(mt5, Path("x.exe")).symbol_facts("EURUSD") is None


def test_close_any_closes_a_terminal_that_was_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``close_terminals`` setting's "all": the person's own terminal is closed too."""
    monkeypatch.setattr("mt5copier.worker.find_terminal_pid", lambda path: 4242)  # already up
    proc = FakeProcess(4242, exe="x.exe")
    monkeypatch.setattr("mt5copier.worker.psutil.Process", lambda pid: proc)
    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    session.disconnect(close_terminal=True, close_any=True)
    assert proc.terminated


def test_close_any_still_needs_close_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain disconnect is the reconnect path, whatever the setting."""
    monkeypatch.setattr("mt5copier.worker.find_terminal_pid", lambda path: 4242)
    proc = FakeProcess(4242, exe="x.exe")
    monkeypatch.setattr("mt5copier.worker.psutil.Process", lambda pid: proc)
    session = _Session(FakeMT5(), Path("x.exe"))
    session.connect()
    session.disconnect(close_any=True)
    assert not proc.terminated


def test_the_worker_passes_close_any_through_from_its_stop_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("mt5copier.worker.find_terminal_pid", lambda path: 4242)
    proc = FakeProcess(4242, exe="x.exe")
    monkeypatch.setattr("mt5copier.worker.psutil.Process", lambda pid: proc)
    run_until_stopped(
        FakeMT5(),
        tmp_path,
        stop_command=Command(kind=CommandKind.STOP, close_terminal=True, close_any=True),
    )
    assert proc.terminated


# --- quotes for the virtual master -----------------------------------------------------------


@dataclass
class FakeSymbol:
    name: str
    visible: bool = False


@dataclass
class FakeTick:
    bid: float
    ask: float
    time: int = 0
    time_msc: int = 0


class QuotingMT5(FakeMT5):
    """A broker that lists decorated names and ticks them."""

    def __init__(self, *, names: tuple[str, ...], ticks: dict[str, FakeTick], **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._names = names
        self._ticks = ticks

    def symbols_get(self) -> tuple[FakeSymbol, ...]:
        return tuple(FakeSymbol(name=n) for n in self._names)

    def symbol_info_tick(self, name: str) -> FakeTick | None:
        return self._ticks.get(name)


def quoting_session() -> _Session:
    mt5 = QuotingMT5(
        names=("EURUSDm", "XAUUSDm", "US30"),
        ticks={"EURUSDm": FakeTick(bid=1.08500, ask=1.08520)},
        symbol_info_data={"EURUSDm": FakeSymbolInfo(name="EURUSDm")},
    )
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    return session


def test_watched_symbols_are_quoted_in_clean_names(tmp_path: Path) -> None:
    session = quoting_session()
    session.watch = WatchCommand(symbols=("EURUSD",), symbol_suffix="m")
    snap = session.poll("TID", seq=1, attempts=0)
    (q,) = snap.quotes
    assert q.symbol == "EURUSD", "answered in the clean name, not the broker's"
    assert q.bid == 1.08500 and q.ask == 1.08520


def test_a_symbol_that_cannot_be_resolved_is_absent_not_zero(tmp_path: Path) -> None:
    session = quoting_session()
    session.watch = WatchCommand(symbols=("EURUSD", "GER40"), symbol_suffix="m")
    snap = session.poll("TID", seq=1, attempts=0)
    assert [q.symbol for q in snap.quotes] == ["EURUSD"]


def test_a_resolved_symbol_that_will_not_tick_is_absent(tmp_path: Path) -> None:
    session = quoting_session()
    session.watch = WatchCommand(symbols=("XAUUSD",), symbol_suffix="m")  # resolves, no tick
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.quotes == ()


def test_a_warm_command_selects_the_symbol_and_quotes_nothing(tmp_path: Path) -> None:
    """Warming puts the ticket's symbol into Market Watch so its first tick has started
    before a trade asks for it -- acted on once per command, and never quoted back."""
    mt5 = QuotingMT5(
        names=("EURUSDm", "XAUUSDm"),
        ticks={},
        symbol_info_data={"XAUUSDm": FakeSymbolInfo(name="XAUUSDm", visible=False)},
    )
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    session.warm = WarmCommand(symbols=("XAUUSD",), symbol_suffix="m")
    snap = session.poll("TID", seq=1, attempts=0)
    assert mt5.symbol_select_calls == ["XAUUSDm"]
    assert snap.quotes == ()
    session.poll("TID", seq=2, attempts=0)
    assert mt5.symbol_select_calls == ["XAUUSDm"], "the same warm command is not re-acted on"


def test_no_watch_means_no_quotes(tmp_path: Path) -> None:
    session = quoting_session()
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.quotes == ()


def test_a_new_watch_command_replaces_the_old_watch(tmp_path: Path) -> None:
    session = quoting_session()
    session.watch = WatchCommand(symbols=("EURUSD",), symbol_suffix="m")
    session.poll("TID", seq=1, attempts=0)
    session.watch = WatchCommand()  # the supervisor cleared it (feed handed over)
    snap = session.poll("TID", seq=2, attempts=0)
    assert snap.quotes == ()


class RatesMT5(QuotingMT5):
    """A broker that also answers bars for the minichart."""

    def __init__(self, *, rates: list[dict[str, float]] | None, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._rates = rates
        self.rates_calls: list[tuple[str, int, int, int]] = []

    def copy_rates_from_pos(
        self, name: str, timeframe: int, start: int, count: int
    ) -> list[dict[str, float]] | None:
        self.rates_calls.append((name, timeframe, start, count))
        return self._rates


def rates_session(rates: list[dict[str, float]] | None) -> tuple[_Session, RatesMT5]:
    mt5 = RatesMT5(
        rates=rates,
        names=("EURUSDm",),
        ticks={"EURUSDm": FakeTick(bid=1.08500, ask=1.08520)},
        symbol_info_data={"EURUSDm": FakeSymbolInfo(name="EURUSDm")},
    )
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    return session, mt5


BAR = {"time": 1_700_000_000, "open": 1.08, "high": 1.09, "low": 1.07, "close": 1.085}


def test_rates_are_read_on_the_resolved_symbol(tmp_path: Path) -> None:
    session, mt5 = rates_session([BAR, {**BAR, "time": 1_700_000_060}])
    got = session.read_rates(RatesRequest(symbol="EURUSD", timeframe=5, count=2, symbol_suffix="m"))
    assert not isinstance(got, str)
    candles, meta = got
    assert mt5.rates_calls == [("EURUSDm", 5, 0, 2)], "read on the broker's own name"
    assert [c.ts for c in candles] == [1_700_000_000, 1_700_000_060]
    assert (candles[0].open, candles[0].high, candles[0].low, candles[0].close) == (
        1.08,
        1.09,
        1.07,
        1.085,
    )
    assert meta is not None
    assert (meta.digits, meta.point) == (5, 0.0001)
    assert meta.value_per_unit == pytest.approx(100_000.0), "the loss-side tick value per 1.0"


def test_rates_for_a_symbol_the_broker_lacks_answer_why(tmp_path: Path) -> None:
    session, mt5 = rates_session([BAR])
    got = session.read_rates(RatesRequest(symbol="GER40", timeframe=5, symbol_suffix="m"))
    assert isinstance(got, str)
    assert mt5.rates_calls == [], "nothing was read"


def test_rates_the_terminal_will_not_give_answer_why(tmp_path: Path) -> None:
    session, _ = rates_session(None)
    got = session.read_rates(RatesRequest(symbol="EURUSD", timeframe=5, symbol_suffix="m"))
    assert got == "the terminal answered no bars"


def test_the_worker_loop_answers_a_rates_request(tmp_path: Path) -> None:
    mt5 = RatesMT5(
        rates=[BAR],
        names=("EURUSDm",),
        ticks={"EURUSDm": FakeTick(bid=1.08500, ask=1.08520)},
        symbol_info_data={"EURUSDm": FakeSymbolInfo(name="EURUSDm")},
    )
    out_q: queue.Queue = queue.Queue()
    cmd_q: queue.Queue = queue.Queue()

    thread = threading.Thread(
        target=run_worker,
        args=("TID12345", "x.exe", out_q, cmd_q, str(tmp_path / "w.log"), 50, [0.01]),
        kwargs={"_session_factory": lambda path: _Session(mt5, path)},
        daemon=True,
    )
    thread.start()
    wait = threading.Event()
    collected: list[object] = []
    # Ask once it is connected, as the engine does: it only asks a connected feed.
    for _ in range(100):
        collected += drain(out_q)
        if any(isinstance(s, Snapshot) and s.state is WorkerState.CONNECTED for s in collected):
            break
        wait.wait(0.05)
    cmd_q.put(RatesRequest(symbol="EURUSD", timeframe=5, count=1, symbol_suffix="m"))
    for _ in range(100):
        collected += drain(out_q)
        if any(isinstance(item, RatesResult) for item in collected):
            break
        wait.wait(0.05)
    cmd_q.put(_PLAIN_STOP)
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "worker did not stop"
    answer = next(item for item in collected if isinstance(item, RatesResult))
    assert answer.error is None and answer.symbol == "EURUSD" and answer.timeframe == 5
    assert [c.close for c in answer.candles] == [1.085]
    assert answer.meta is not None and answer.meta.digits == 5, "the pricing facts ride along"


def test_a_rates_request_while_disconnected_answers_why(tmp_path: Path) -> None:
    mt5 = RatesMT5(
        rates=[BAR],
        names=("EURUSDm",),
        ticks={},
        symbol_info_data={},
        init_ok=False,
    )
    out_q: queue.Queue = queue.Queue()
    cmd_q: queue.Queue = queue.Queue()
    cmd_q.put(RatesRequest(symbol="EURUSD", timeframe=5, symbol_suffix="m"))
    thread = threading.Thread(
        target=run_worker,
        args=("TID12345", "x.exe", out_q, cmd_q, str(tmp_path / "w.log"), 50, [0.01]),
        kwargs={"_session_factory": lambda path: _Session(mt5, path)},
        daemon=True,
    )
    thread.start()
    wait = threading.Event()
    collected: list[object] = []
    for _ in range(100):
        collected += drain(out_q)
        if any(isinstance(item, RatesResult) for item in collected):
            break
        wait.wait(0.05)
    cmd_q.put(_PLAIN_STOP)
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "worker did not stop"
    answer = next(item for item in collected if isinstance(item, RatesResult))
    assert answer.error == "the feed terminal is not connected"


def test_the_worker_loop_applies_a_watch_command(tmp_path: Path) -> None:
    mt5 = QuotingMT5(
        names=("EURUSDm",),
        ticks={"EURUSDm": FakeTick(bid=1.08500, ask=1.08520)},
        symbol_info_data={"EURUSDm": FakeSymbolInfo(name="EURUSDm")},
    )
    out_q: queue.Queue = queue.Queue()
    cmd_q: queue.Queue = queue.Queue()
    cmd_q.put(WatchCommand(symbols=("EURUSD",), symbol_suffix="m"))

    thread = threading.Thread(
        target=run_worker,
        args=("TID12345", "x.exe", out_q, cmd_q, str(tmp_path / "w.log"), 50, [0.01]),
        kwargs={"_session_factory": lambda path: _Session(mt5, path)},
        daemon=True,
    )
    thread.start()
    wait = threading.Event()
    collected: list[Snapshot] = []
    for _ in range(100):
        collected += drain(out_q)
        if any(s.quotes for s in collected):
            break
        wait.wait(0.05)
    cmd_q.put(_PLAIN_STOP)
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "worker did not stop"
    quoted = [s for s in collected if s.quotes]
    assert quoted and quoted[0].quotes[0].symbol == "EURUSD"
