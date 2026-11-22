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

ACCOUNT_TRADE_MODE_DEMO = 0
"""``account_info().trade_mode``'s demo value. Contest is 1, real is 2.

Lives here, not in :mod:`mt5copier.executor`, so the pure read-only check in
:mod:`mt5copier.presentation` (a slave's "held back: not a demo account" state) and the
executor's own send-time refusal (:meth:`Executor._guard_live_account`) compare against the same
constant instead of two copies of the same magic number drifting apart."""

COPIER_MAGIC = 260919
"""Stamped on every order this copier sends, so it can recognise its own positions.

This is an identity, not a key: it says "ours", never *which* master trade it came from --
a single 64-bit field cannot reliably carry both, so ``store.py``'s table is the authority on
that. Its one job is letting startup reconciliation tell our forgotten positions (orphans,
which must be surfaced) from the user's own trading on the same terminal (none of our
business).

**Fixed once live copies carry it.** Changing it makes every existing copy unrecognisable --
they would read as someone else's trades and be silently ignored rather than reported. Chosen
2026-09-19 against terminals running no EAs, so nothing else on them uses a magic at all.
"""


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


@dataclass(frozen=True, slots=True)
class PositionLive:
    """The per-tick half of a position: what :class:`Position` deliberately leaves out.

    Kept apart so that nothing the reconciler compares changes on every tick. This is for
    display only -- nothing in ``reconcile.py`` or the ledger may read it. Each value is None
    when the broker did not supply it, which never fails the read: a missing P/L is a blank
    cell, whereas a missing position is a CLOSE.
    """

    ticket: int
    price_current: float | None = None
    profit: float | None = None
    swap: float | None = None


@dataclass(frozen=True, slots=True)
class Deal:
    """One entry in the account's trade history, as far as it moved the balance.

    Read for the guards, which rebuild the balance at an earlier moment -- a daily reset -- by
    taking off what every later deal added. That is why every kind of deal is kept, not only
    trades: a deposit or a withdrawal moves the balance as surely as a closed loss does.
    """

    ticket: int
    time: float
    """When it happened, in real UTC seconds -- converted from the broker's own clock, which
    MT5 reports as if it were UTC (see ``Snapshot.server_offset_s``)."""
    type: int
    """MT5's ``DEAL_TYPE_*``: 0 buy, 1 sell, 2 balance, and the rest."""
    entry: int
    """MT5's ``DEAL_ENTRY_*``: 0 in, 1 out, 2 in/out reversal, 3 out by."""
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    symbol: str = ""
    position: int = 0
    """The position the deal opened or closed -- the open position's ticket -- or 0 for a balance
    operation. What tells a guard which trades were open at an earlier moment."""

    @property
    def net(self) -> float:
        """What this deal added to the balance."""
        return self.profit + self.commission + self.swap + self.fee


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

    @property
    def flipped(self) -> OrderType | None:
        """The other resting type on the same side: a limit as a stop and back.

        The side of the quote a pending must rest on is the one thing that separates the
        two -- a buy limit below the ask, a buy stop above it -- so a price the broker
        refuses for one of them is, whenever the refusal is about which side the quote is
        on, exactly where the other may rest. None for anything that is not a plain limit
        or stop, a stop-limit included: its trigger and entry pair has no mirror."""
        return _FLIPPED.get(self)


_BUY_ORDERS = frozenset(
    {OrderType.BUY, OrderType.BUY_LIMIT, OrderType.BUY_STOP, OrderType.BUY_STOP_LIMIT}
)

_FLIPPED = {
    OrderType.BUY_LIMIT: OrderType.BUY_STOP,
    OrderType.BUY_STOP: OrderType.BUY_LIMIT,
    OrderType.SELL_LIMIT: OrderType.SELL_STOP,
    OrderType.SELL_STOP: OrderType.SELL_LIMIT,
}


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
    expiration_utc: int = 0
    """When the broker will delete this order if it has not filled, in **real UTC seconds** --
    the worker converts it from the broker's server clock before it gets here. 0 for an order
    with no copyable expiry: good-till-cancelled, a day order (whose "day" is the broker's,
    not a timestamp), or an expiry read before the broker's clock offset was known."""

    @property
    def has_stop(self) -> bool:
        return self.sl != 0.0


@dataclass(frozen=True, slots=True)
class Quote:
    """One watched symbol's current price, as the feed worker read it.

    ``symbol`` is the *clean* canonical name the supervisor asked for, never the broker's
    decorated one -- the worker resolves the decoration on its side and answers in the name
    the virtual master's book is kept in, so nothing upstream needs to know what the feed
    broker calls the instrument.
    """

    symbol: str
    bid: float
    ask: float
    ts: float = 0.0
    """When the worker read it, in its wall clock. Display only: staleness is judged from the
    snapshot that carried it, as everything else on a snapshot is."""


@dataclass(frozen=True, slots=True)
class WatchCommand:
    """Supervisor -> worker: quote these symbols in every snapshot from now on.

    Latest wins -- each command replaces the whole watch list rather than editing it, so a
    worker that missed one is wrong for a tick, not forever. Symbols are clean canonical
    names; the slave's own decoration travels alongside so the worker can resolve them
    against its broker's list exactly as the executor resolves a trade's symbol.
    """

    symbols: tuple[str, ...] = ()
    symbol_prefix: str = ""
    symbol_suffix: str = ""
    symbol_map: tuple[tuple[str, str], ...] = ()
    """Sorted ``(clean, broker)`` pairs, a tuple for the same reason as
    :attr:`OrderCommand.symbol_map`."""


CHART_TIMEFRAMES: tuple[tuple[str, int], ...] = (
    ("M1", 1),
    ("M5", 5),
    ("M15", 15),
    ("M30", 30),
    ("H1", 16385),
)
"""The minichart's timeframes, each with MT5's own ``TIMEFRAME_*`` value (H1 is ``0x4001``),
restated rather than imported for the same reason as :class:`PositionType`."""


@dataclass(frozen=True, slots=True)
class Candle:
    """One closed (or still-forming last) bar, as the feed terminal read it."""

    ts: int
    """The bar's open time in the broker server's epoch seconds. Display only, like
    :attr:`Quote.ts` -- nothing is scheduled off it."""
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class RatesRequest:
    """Supervisor -> feed worker: read the last ``count`` bars of one symbol, once.

    Not a subscription: the supervisor asks again when it wants fresher bars, so a request
    lost with a worker costs one refresh, not the chart. ``symbol`` is clean, with the
    slave's decoration alongside, exactly as on :class:`WatchCommand`."""

    symbol: str
    timeframe: int
    """One of :data:`CHART_TIMEFRAMES`'s values."""
    count: int = 120
    symbol_prefix: str = ""
    symbol_suffix: str = ""
    symbol_map: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SymbolMeta:
    """How the feed terminal prices one symbol, for the ticket's display arithmetic.

    Display only -- real sizing happens on each slave's own terminal. ``value_per_unit`` is
    what one lot gains or loses over a 1.0 move of the quote, in the *feed* account's
    currency (``trade_tick_value_loss / trade_tick_size``), so the page's lot estimate is
    at least the right order of magnitude; 0.0 when the feed could not price it."""

    digits: int = 0
    point: float = 0.0
    value_per_unit: float = 0.0


@dataclass(frozen=True, slots=True)
class RatesResult:
    """Feed worker -> supervisor: the bars asked for, oldest first -- or why not.

    Droppable like a snapshot, not precious like an :class:`OrderResult`: the next refresh
    supersedes it."""

    terminal_id: str
    symbol: str
    timeframe: int
    candles: tuple[Candle, ...] = ()
    error: str | None = None
    ts: float = 0.0
    """When the worker read them, in its wall clock. Display only."""
    meta: SymbolMeta | None = None
    """The symbol's pricing facts, read in the same pass as the bars; None when unknown."""


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


class ActionKind(StrEnum):
    """What the copier intends to do about one master position on one slave.

    Lives here rather than in :mod:`mt5copier.reconcile` because it is the vocabulary of the
    process boundary as well as of the diff: the engine decides an :class:`ActionKind` and the
    worker executes one. Two enums kept in sync by hand would eventually drift.
    """

    OPEN = "open"
    CLOSE = "close"
    """The master no longer holds it. A full close is never conditional."""
    REDUCE = "reduce"
    """The master partially closed; mirror the same fraction. Volume only ever moves down --
    on a hedging account each entry is its own ticket, so an open position's volume cannot be
    added to, only reduced."""
    BOOK = "book"
    """The copy's breakeven fired and its slave books profit: close the slave's set percent of
    the position, once. The copier's own decision rather than a mirror of the master, which is
    why it is not a ``REDUCE``: the two are de-duplicated and retried separately, and a
    master's later partial is still mirrored as a fraction of what is left."""
    MODIFY_SLTP = "modify_sltp"

    # --- pending orders ---------------------------------------------------------------------
    PLACE = "place"
    """Mirror a resting limit/stop order the master has placed."""
    MODIFY_ORDER = "modify_order"
    """The master moved a resting order's entry price, stop or target. Unlike a position, a
    pending order's *entry* can change, which is why this is a separate kind."""
    CANCEL = "cancel"
    """The master deleted a resting order before it filled."""
    ORDER_FILLED = "order_filled"
    """The master's order filled. Reported but never sent: the slave's own copy of the order
    rests at the same price and fills on its own. What this does is hand tracking over from
    the order to the position it became."""


DISPATCHABLE_KINDS = frozenset(
    {
        ActionKind.OPEN,
        ActionKind.PLACE,
        ActionKind.CLOSE,
        ActionKind.REDUCE,
        ActionKind.BOOK,
        ActionKind.MODIFY_SLTP,
        ActionKind.MODIFY_ORDER,
        ActionKind.CANCEL,
    }
)
"""Which actions this build can actually send, as opposed to merely derive.

Every kind the executor implements, which since Stage 3 is all of them but ``ORDER_FILLED`` --
and that one is not a gap. It is a report about the slave's own fill, whose whole effect is to
hand tracking from an order to the position it became; there is nothing to send anywhere.

The set is still threaded through ``CopyLedger.step`` rather than assumed, because it is what
keeps an unsendable kind out of ``_inflight``: an action left waiting for an answer nothing
will give freezes that ticket until the in-flight timeout. Narrowing it again is therefore
safe -- but it must not be silent, which is what ``Engine._warn_undispatchable`` is for.
"""


class SizingBasis(StrEnum):
    """How the worker must compute the lots for an OPEN or a PLACE.

    Decided by the diff engine, because it follows from the master's trade and the slave's
    config -- both pure data. The worker executes the choice; it does not make it.
    """

    RISK_FROM_STOP = "risk_from_stop"
    """The normal path: the slave's own risk budget, spread over the master's entry-to-stop
    distance, net of that slave's commission and slippage."""

    MASTER_LOT_MULTIPLE = "master_lot_multiple"
    """No stop to measure, so the master's own lot size scaled by ``no_stop_lot_multiple``.
    The one case where a slave inherits the master's sizing, and opt-in per slave."""


class CommandKind(StrEnum):
    STOP = "stop"
    RECONNECT = "reconnect"
    PING = "ping"


@dataclass(frozen=True, slots=True)
class Command:
    """Supervisor -> worker."""

    kind: CommandKind
    seq: int = 0
    close_terminal: bool = False
    """STOP only: also close the terminal process, not just the MT5 session.

    Only ever true when the supervisor is done with this terminal for good (the whole app is
    stopping, or the terminal was unassigned) -- never for a restart, which wants the same
    terminal back a moment later. The worker only acts on this if it is also the one that
    launched the terminal in the first place, unless ``close_any`` is set too; see
    :mod:`mt5copier.worker`."""

    close_any: bool = False
    """STOP with ``close_terminal`` only: close the terminal even if it was already running when
    the worker connected -- the person's own. The ``close_terminals`` setting's "all"."""


@dataclass(frozen=True, slots=True)
class TradeConstraint:
    """One cap the guards put on this trade, carried with the command so the worker can hold
    the trade to it after sizing, without reading config or catalog.

    The engine decides the number -- a margin allowance, what is left of a total, a class's
    lots ceiling -- and the worker measures the trade against it, because only the worker can
    ask its broker what the trade would cost. A new kind of per-trade limit is a new ``kind``
    here and its arithmetic in :mod:`mt5copier.sizing`; nothing else on the wire changes."""

    kind: str
    """What is limited: ``"lots"``, ``"margin"`` or ``"total_margin"``. A kind this build's
    worker does not know is declined, never ignored -- the guard asked for a limit, and
    sending the trade unmeasured is the unsafe side."""
    cap: float
    """The limit, in the kind's own unit: lots, or the account's money."""
    mode: str
    """What to do with a trade over the cap: ``"decline"`` it, ``"shrink"`` it to fit, or
    ``"warn"`` and send it as sized."""
    rule: str = ""
    """The title of the guard behind the cap, so the worker's answer can name it."""
    rule_id: str = ""
    allowed: str = ""
    """The engine's reading, for the guard note: ``US30 -> indices, capped at 1 lots a
    trade``. Always names the class, so a misfiled symbol is visible, never silent."""


@dataclass(frozen=True, slots=True)
class OrderCommand:
    """Supervisor -> worker: one thing to do on this slave's terminal.

    **Self-contained on purpose.** Every value the worker needs is carried here, including the
    already-resolved risk budget and the slave's cost settings, so the worker never reads
    config. Workers are spawned processes with a copy-on-write snapshot of the world; a
    setting the user changed two minutes ago would otherwise be honoured by the engine and
    ignored by the worker, which is the kind of divergence nobody notices until it matters.

    The division of labour: the engine decides *what* and *how much money*, because it has the
    config and the master's state; the worker decides *how many lots*, because only it can ask
    the slave's terminal what one lot would lose.
    """

    command_id: int
    op: ActionKind
    master_ticket: int
    master_symbol: str
    direction: PositionType
    """The direction on the slave -- already inverted if this slave reverse-copies."""

    order_type: OrderType | None = None
    """Set for PLACE / MODIFY_ORDER / CANCEL; None for position operations."""

    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    """Slave-facing levels: already swapped if this slave reverse-copies."""

    expiration_utc: int = 0
    """PLACE / MODIFY_ORDER only: the master order's expiry in real UTC seconds, 0 for none.
    The worker converts it back into *its own* broker's server clock when it sends, because
    each side's terminal speaks its own server time."""

    master_volume: float = 0.0
    sizing_basis: SizingBasis = SizingBasis.RISK_FROM_STOP
    lot_multiple: float | None = None

    risk_amount: float = 0.0
    """Money to risk, in this slave's account currency, already derived from its balance and
    risk setting by the engine."""

    commission_per_lot: float = 0.0
    slippage_points: int = 0
    max_lot: float | None = None
    deviation_points: int = 20

    pending_fallback: bool = True
    """PLACE only: when the broker refuses the pending's price, retry it once as the other
    resting type (a limit as a stop, a stop as a limit) at the same price, and if that is
    refused over its price too, execute at market. The slave's ``pending_fallback`` setting."""

    close_fraction: float | None = None
    """REDUCE and BOOK only."""
    slave_ticket: int | None = None
    """The slave's own ticket, for anything that acts on an existing position or order."""

    symbol_prefix: str = ""
    symbol_suffix: str = ""
    """This slave's configured symbol decoration, carried rather than looked up for the same
    reason as everything else here."""

    symbol_map: tuple[tuple[str, str], ...] = ()
    """This slave's explicit per-symbol overrides, as sorted ``(master, slave)`` pairs.

    A tuple of pairs rather than a dict because this dataclass is frozen: a mutable field would
    make it unhashable and quietly break the promise the decorator advertises. The engine sorts
    them so two otherwise-identical commands compare equal, and the executor rebuilds the dict
    on the far side."""

    magic: int = 0
    comment: str = ""
    allow_live: bool = False
    """Explicit per-slave override of the demo-only guard. Absent this, the worker refuses to
    trade any account that is not a demo -- see :mod:`mt5copier.executor`."""
    constraints: tuple[TradeConstraint, ...] = ()
    """OPEN / PLACE only: the guards' per-trade caps, applied in this order by the worker's
    fit loop, each against the lots the one before let through. Empty for no limits, and for
    every command that opens nothing."""
    to_breakeven: bool = False
    """MODIFY_SLTP on the master only: move its stop to breakeven, past ``entry`` by the
    position's own commission and ``slippage_points``, worked out by the worker. ``sl`` and
    ``tp`` are the master's levels now: a stop already past breakeven is left alone, never
    loosened, and the target is kept."""
    tp_net_rr: bool = False
    """Direct trading only, on anything that writes a target: the target encodes the ticket's
    risk:reward, so the worker lands the copy's target past it by (rr + 1) x its own
    commission. Commission is paid win or lose -- the win collects reward minus one, the loss
    costs risk plus one -- and that distance is exactly what keeps the realized reward:risk at
    the ticket's rr. Priced by the worker, like breakeven's buffer, because only the terminal
    can say what a price move is worth in its account's money; unpriceable means the target
    goes unpadded, never guessed."""


@dataclass(frozen=True, slots=True)
class GuardNote:
    """What a prop-firm guard did to one trade, or to the slave: ``Margin per trade shrunk it
    from 3.5 to 1.2 lots: 6,130.40 margin needed, 2,101.97 allowed``.

    Said wherever the trade is shown -- the day book, its history card, the guards page -- so
    a trade a guard changed never looks like one the copier sized that way by itself."""

    rule: str
    """The rule's title."""
    action: str
    """What it did, in a word or two: ``shrunk``, ``declined``, ``warned``."""
    detail: str
    rule_id: str = ""

    def text(self) -> str:
        return f"{self.rule} {self.action} it: {self.detail}"


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Worker -> supervisor: what actually happened.

    ``filled_volume`` is what the broker reports, never what was asked for. A partial fill
    that is recorded as the requested size leaves the copier believing in volume that does not
    exist, and every later partial close is then computed against a fiction.
    """

    command_id: int
    terminal_id: str
    op: ActionKind
    master_ticket: int
    ok: bool

    retcode: int | None = None
    outcome: str = ""
    slave_ticket: int | None = None
    slave_symbol: str | None = None
    requested_volume: float | None = None
    filled_volume: float | None = None
    price: float | None = None
    attempts: int = 0
    error: str | None = None
    point: float | None = None
    """The slave symbol's point size, on a filled OPEN or PLACE, so slippage can be told in
    points without the engine asking the terminal. Also kept on the copy record, where it is
    what a breakeven's slippage buffer is counted in."""
    digits: int | None = None
    """The slave symbol's digits, alongside ``point``, for rounding a breakeven stop."""
    price_value: float | None = None
    """On a filled OPEN or PLACE: what a 1.0 move in price is worth on one lot, in the account
    currency. Kept on the copy record, where it turns the slave's commission into a breakeven
    distance."""
    commission: float | None = None
    """On a filled OPEN: the commission charged on the opening deal, as a positive cost in the
    account currency. None when it could not be read or none was charged; the copy then falls
    back to the slave's ``commission_per_lot_round_turn``."""

    placed_type: OrderType | None = None
    """On a filled PLACE: the resting type actually placed, when the fallback flipped it away
    from the master's -- a limit placed as a stop or the reverse. None when the copy rests as
    the master's own type. Belief records this type, because it is what the slave carries."""

    filled_at_market: bool = False
    """A PLACE that could not rest at all and fell back to market execution. The copy is an
    open position from the start, never a resting order, and the ledger must remember it as
    one: its master is still a resting order, so it waits on that order's fate -- closed if
    the order is cancelled, managed normally once the order fills."""

    unsplittable: bool = False
    """A ``BOOK`` that was not sent because the slice, or what it would leave open, is under
    the symbol's minimum lot. Reported as a failure, but a final one: the position stays whole
    and is never booked, rather than closed outright."""

    target_gone: bool = False
    """The position or order this acted on does not exist any more.

    Reported with ``ok`` true, because closing and cancelling describe a state that has now
    been reached without us. For a **modification** it means the opposite of a success: the
    levels were not applied to anything, so belief must not record them. Without this the
    ledger would write a stop the slave never carried -- and since the diff compares the
    master against belief and never against the slave, nothing would look again."""

    guard: tuple[GuardNote, ...] = ()
    """What the guards' margin caps did to an OPEN or a PLACE: shrunk it, declined it, or
    warned and sent it as sized. Empty when no cap was reached."""

    retryable: bool = False
    """Whether a failure was transient -- a link that was down, a queue that would not take
    the command -- rather than the broker refusing the request.

    Only meaningful when ``ok`` is false, and it describes the failure rather than deciding
    what to do about it. The ledger owns that decision: a transient failure is re-derived on
    the next tick, a refusal is left to stand so the identical request is not sent into the
    identical refusal forever. An OPEN or a PLACE is never re-derived either way -- by the
    time a link comes back, the copy would be filling far from the master's entry."""

    def describe(self) -> str:
        head = f"{self.op.value.upper():<12} #{self.master_ticket}"
        if not self.ok:
            return f"{head} FAILED: {self.error or self.outcome}"
        where = f" {self.slave_symbol}" if self.slave_symbol else ""
        tries = f" ({self.attempts} attempts)" if self.attempts > 1 else ""
        if self.op in (ActionKind.MODIFY_SLTP, ActionKind.MODIFY_ORDER):
            # A modification fills nothing and creates no ticket, so MT5 answers with zeroes.
            # Reporting those as "filled 0 @0" would read like a trade that went nowhere
            # instead of a stop that moved.
            return f"{head}{where} {self.outcome}{tries}"
        vol = f"{self.filled_volume:g}" if self.filled_volume is not None else "?"
        price = f" @{self.price:g}" if self.price else ""
        return f"{head}{where} filled {vol}{price}{tries}"


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

    worker_pid: int | None = None
    """The sending worker's process id, stamped on publish. ``seq`` restarts at 0 with every
    worker, so it only orders snapshots *within* one worker; this is what tells a leftover from
    a worker already stopped apart from its replacement."""

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
    margin: float | None = None
    """Margin in use, in the account currency. Display only, like the two below."""
    margin_level: float | None = None
    """Equity / margin as a percentage. MT5 reports 0 when no margin is in use."""
    profit: float | None = None
    """Floating P/L across every open position, in the account currency."""

    # From terminal_info()
    terminal_connected: bool | None = None
    terminal_build: int | None = None
    ping_last_us: int | None = None
    """``terminal_info().ping_last``: the last ping to the trade server, in microseconds."""

    # From positions_get()
    positions: int | None = None
    """Count of open positions, or None when the read failed."""

    open_positions: tuple[Position, ...] | None = None
    """The open positions themselves. **None means "unknown", not "none open"** -- an empty
    tuple is a flat account, while None is a failed read.

    The distinction is load-bearing: the reconciler derives a CLOSE for every copy it cannot
    find on the master, so feeding it an empty list after a failed read would propose closing
    every copied position at once. Consumers must treat None as "do not reconcile this tick"."""

    live_positions: tuple[PositionLive, ...] | None = None
    """Floating P/L, current price and swap per open position, read by the same
    ``positions_get()`` call as ``open_positions`` and None exactly when it is. Display only:
    see :class:`PositionLive` for why it is not part of :class:`Position`."""

    pending_orders: tuple[PendingOrder, ...] | None = None
    """Resting limit/stop orders, with the same None-means-unknown rule as ``open_positions``,
    and for a sharper reason: a vanished order is read as a cancellation, so an empty list
    after a failed read would propose cancelling every copied order at once."""

    server_offset_s: int | None = None
    """How far the broker's clock runs ahead of UTC, in seconds (negative if behind). MT5
    reports every time -- ticks, deals -- in the broker's own clock as if it were UTC, so this
    is what turns them into real time. None until a live price has been seen: it is measured
    off a tick that has just changed, never off a stale one, which on a closed market could be
    days old. Read for the guards."""

    deals: tuple[Deal, ...] | None = None
    """The account's deals from ``deals_since`` on, oldest first, with times in real UTC.
    **None means "unknown"**, not "no deals": a guard that rebuilt today's opening balance
    from an empty list after a failed read would report every loss today as never having
    happened. Refreshed every few seconds, and at once when the balance changes."""

    deals_since: float | None = None
    """The UTC start of the window ``deals`` covers. A balance can only be rebuilt for a moment
    inside it."""

    quotes: tuple[Quote, ...] = ()
    """Current prices for the symbols a :class:`WatchCommand` asked this worker to watch,
    keyed by their clean names. Empty when nothing is watched, when the terminal is not
    connected, or when a watched symbol cannot be resolved or quoted -- a missing quote is
    "unknown", and the virtual master refuses to fill against unknown rather than guessing."""

    symbols: tuple[str, ...] | None = None
    """This broker's whole symbol catalog, pushed once per connection -- on the first good
    snapshot after a (re)connect -- and ``None`` on every other snapshot, so the list rides
    an existing message instead of needing a command and a reply (the design ROADMAP.md
    records for the dropped mapping tester). The UI's symbol search reads the cached copy."""

    error: str | None = None
    """Human-readable last error, kept for display even after a later success."""

    connect_attempts: int = 0

    trade_mode: int | None = None
    """``account_info().trade_mode`` -- compare against :data:`ACCOUNT_TRADE_MODE_DEMO`. None
    when it could not be read, which the executor's own guard treats as *not* demo ("we could
    not tell" is not a reason to trade someone's funded account) -- see
    :meth:`mt5copier.presentation.row_info`, which applies the same policy for display.

    Before this, nothing carried the demo/live fact to the supervisor at all; only the worker
    process could see it, and only at send time."""

    @property
    def is_live(self) -> bool:
        return self.state is WorkerState.CONNECTED
