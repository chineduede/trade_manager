"""Messages exchanged between the supervisor and its per-terminal worker processes.

Plain dataclasses rather than pydantic models: these cross a ``multiprocessing.Queue`` on every
poll tick, so they are pickled constantly and validation on the hot path buys nothing -- both
ends are our own code. Config, which is parsed from user-editable disk, is where validation
belongs.

Everything here must stay picklable and must not import MetaTrader5, because the supervisor
imports this module and the supervisor process must never load the MT5 extension. Loading it in
the parent would create a process-global connection in the one process that is supposed to hold
none.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


class PositionType(IntEnum):
    """Direction of an open position.

    The values are MT5's own ``POSITION_TYPE_BUY`` / ``POSITION_TYPE_SELL``, restated here
    rather than imported, because this module must not load the extension. They are part of
    MT5's wire format and do not change.
    """

    BUY = 0
    SELL = 1


@dataclass(frozen=True, slots=True)
class Position:
    """One open position, as the diff engine needs to see it.

    A flat copy of the fields ``positions_get()`` returns that the reconciler actually uses --
    not the raw namedtuple, which is not guaranteed picklable across MT5 versions and carries
    a dozen fields (swap, profit, price_current) that change on every tick. Copying only the
    stable subset means two snapshots compare equal whenever the *position* is unchanged,
    instead of differing because the floating P/L moved.
    """

    ticket: int
    symbol: str
    type: PositionType
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    time: int = 0
    magic: int = 0
    comment: str = ""

    @property
    def has_stop(self) -> bool:
        """MT5 reports "no stop loss" as ``0.0``, not as null."""
        return self.sl != 0.0


class OrderType(IntEnum):
    """MT5's ``ORDER_TYPE_*`` values, restated rather than imported for the same reason as
    :class:`PositionType`."""

    BUY = 0
    SELL = 1
    BUY_LIMIT = 2
    SELL_LIMIT = 3
    BUY_STOP = 4
    SELL_STOP = 5
    BUY_STOP_LIMIT = 6
    SELL_STOP_LIMIT = 7

    @property
    def is_pending(self) -> bool:
        """BUY and SELL are market orders in flight, not resting orders."""
        return self >= OrderType.BUY_LIMIT

    @property
    def is_stop_limit(self) -> bool:
        return self in (OrderType.BUY_STOP_LIMIT, OrderType.SELL_STOP_LIMIT)

    @property
    def direction(self) -> PositionType:
        """The position this order becomes if it fills."""
        return PositionType.BUY if self in _BUY_ORDERS else PositionType.SELL


_BUY_ORDERS = frozenset(
    {OrderType.BUY, OrderType.BUY_LIMIT, OrderType.BUY_STOP, OrderType.BUY_STOP_LIMIT}
)


@dataclass(frozen=True, slots=True)
class PendingOrder:
    """One resting limit/stop order, as the diff engine needs to see it.

    ``volume`` is ``volume_current`` -- what is still unfilled. Same stable-subset reasoning as
    :class:`Position`: ``price_current`` moves on every tick and is deliberately not copied, so
    two snapshots compare equal whenever the *order* is unchanged.
    """

    ticket: int
    symbol: str
    type: OrderType
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    price_stoplimit: float = 0.0
    time_setup: int = 0
    magic: int = 0
    comment: str = ""

    @property
    def has_stop(self) -> bool:
        return self.sl != 0.0


class WorkerState(StrEnum):
    """Lifecycle of a single terminal connection."""

    STARTING = "starting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    """Reached the terminal, but it is not currently connected to its broker."""
    FAILED = "failed"
    """Could not initialize -- usually a missing exe or a terminal refusing automation."""
    STOPPING = "stopping"
    STOPPED = "stopped"


class CommandKind(StrEnum):
    STOP = "stop"
    RECONNECT = "reconnect"
    PING = "ping"


@dataclass(frozen=True, slots=True)
class Command:
    """Supervisor -> worker."""

    kind: CommandKind
    seq: int = 0


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Worker -> supervisor: everything the UI needs about one terminal.

    ``ts`` is set by the worker, so the supervisor can tell a wedged worker (snapshots have
    stopped arriving) from a disconnected terminal (snapshots still arriving, but
    ``state`` is DISCONNECTED). Conflating those two was what made ``zele-mt5``'s
    connection failures invisible until a trade silently failed to copy.
    """

    terminal_id: str
    state: WorkerState
    seq: int = 0
    ts: float = field(default_factory=time.time)
    """Wall-clock stamp from the worker, for display. Note that staleness is judged by the
    supervisor from its own arrival time, not from this: ``time.monotonic()`` is not defined
    to be comparable across processes, and wall clocks can step."""

    # From account_info()
    login: int | None = None
    server: str | None = None
    company: str | None = None
    currency: str | None = None
    balance: float | None = None
    equity: float | None = None
    margin_free: float | None = None
    leverage: int | None = None
    trade_allowed: bool | None = None

    # From terminal_info()
    terminal_connected: bool | None = None
    terminal_build: int | None = None

    # From positions_get()
    positions: int | None = None
    """Count of open positions, or None when the read failed."""

    open_positions: tuple[Position, ...] | None = None
    """The open positions themselves. **None means "unknown", not "none open"** -- an empty
    tuple is a flat account, while None is a failed read.

    The distinction is load-bearing: the reconciler derives a CLOSE for every copy it cannot
    find on the master, so feeding it an empty list after a failed read would propose closing
    every copied position at once. Consumers must treat None as "do not reconcile this tick"."""

    pending_orders: tuple[PendingOrder, ...] | None = None
    """Resting limit/stop orders, with the same None-means-unknown rule as ``open_positions``,
    and for a sharper reason: a vanished order is read as a cancellation, so an empty list
    after a failed read would propose cancelling every copied order at once."""

    error: str | None = None
    """Human-readable last error, kept for display even after a later success."""

    connect_attempts: int = 0

    @property
    def is_live(self) -> bool:
        return self.state is WorkerState.CONNECTED
