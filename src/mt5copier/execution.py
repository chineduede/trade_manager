"""Order-request decisions that need no connection: filling mode, and what a retcode means.

Both are pure lookups over MT5 constants, and both are places the obvious implementation is
wrong in a way that only shows up against a real broker. The bottom of the module holds the
two scraps both brokers' back ends share verbatim -- the result shell every executor answer
is built on, and the retry backoff both workers pace by -- said once here rather than copied,
because an edit to a copy is how the two brokers drift apart.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from .logging_setup import get_logger
from .protocol import OrderCommand, OrderResult

log = get_logger(__name__)


class TradeAction(IntEnum):
    """``TRADE_ACTION_*`` -- the ``action`` field of a trade request.

    Note ``SLTP`` and ``MODIFY`` are different operations: SLTP changes a *position's* stop
    and target, MODIFY changes a pending *order's* parameters including its entry price.
    A copier without a MODIFY path cannot move a resting order.
    """

    DEAL = 1
    """Market execution -- opening, closing, or reducing a position."""
    PENDING = 5
    SLTP = 6
    MODIFY = 7
    REMOVE = 8
    """Delete a pending order."""


class OrderFilling(IntEnum):
    """``ORDER_FILLING_*`` -- what goes *into* a trade request.

    Note the values are not the same numbers as the permission bits in
    :class:`SymbolFilling`, even though both are "filling mode". Conflating the two is easy
    and produces requests the broker rejects for reasons that look unrelated.
    """

    FOK = 0
    IOC = 1
    RETURN = 2


class SymbolFilling(IntEnum):
    """``SYMBOL_FILLING_*`` -- the bitmask ``symbol_info.filling_mode`` returns.

    A *mask of what is permitted*, not a mode to use. FOK is bit 0 and IOC is bit 1; RETURN
    has no bit, because it is not a market-execution mode at all.
    """

    FOK = 1
    IOC = 2


def choose_filling(mask: int, *, is_pending: bool) -> OrderFilling:
    """Pick a filling mode the symbol actually permits.

    Pending orders take RETURN: the mask describes market execution, and a resting order is
    not executed at placement time.

    For market orders, prefer FOK -- all-or-nothing keeps the copy's size equal to the size
    that was computed, where IOC can leave a partial fill whose volume has to be tracked
    separately. Fall back to IOC when FOK is not permitted.

    A mask of zero means the terminal told us nothing useful (an unselected symbol will do
    this). IOC is the safer guess there: it is the more widely permitted of the two, and a
    wrong guess is recoverable -- ``TRADE_RETCODE_INVALID_FILL`` is retried with the
    alternative rather than being fatal.

    Hardcoding IOC and never reading the mask is the classic copier mistake: on an
    FOK-only symbol it rejects *every* copied order.
    """
    if is_pending:
        return OrderFilling.RETURN
    if mask & SymbolFilling.FOK:
        return OrderFilling.FOK
    if mask & SymbolFilling.IOC:
        return OrderFilling.IOC
    log.debug("symbol reports filling mask %r; defaulting to IOC", mask)
    return OrderFilling.IOC


def alternative_filling(tried: OrderFilling, mask: int) -> OrderFilling | None:
    """The other mode to try after ``INVALID_FILL``, or None if there isn't one.

    Only ever offers a mode the mask permits, so a rejection loop cannot bounce between two
    modes the symbol refuses equally.
    """
    if tried is OrderFilling.FOK and mask & SymbolFilling.IOC:
        return OrderFilling.IOC
    if tried is OrderFilling.IOC and mask & SymbolFilling.FOK:
        return OrderFilling.FOK
    if tried is not OrderFilling.IOC and mask == 0:
        # Nothing was reported, so the first choice was a guess; the other market mode is
        # still worth exactly one attempt.
        return OrderFilling.IOC
    return None


class OrderTime(IntEnum):
    """``ORDER_TIME_*`` -- a request's ``type_time`` field: how long a pending order rests.

    The copier only ever sends GTC and SPECIFIED. DAY and SPECIFIED_DAY expire at a *trading
    day's* end, and whose day that is depends on the broker's server clock -- the master's day
    and the slave's can end hours apart -- so neither is mirrored; the master's own expiry
    still reaches the slave as a CANCEL when the order leaves the master's book.
    """

    GTC = 0
    DAY = 1
    SPECIFIED = 2
    SPECIFIED_DAY = 3


class SymbolExpiration(IntEnum):
    """``SYMBOL_EXPIRATION_*`` -- the bitmask ``symbol_info.expiration_mode`` returns.

    Like :class:`SymbolFilling`, a *mask of what is permitted*, and its bits are not the
    :class:`OrderTime` values: SPECIFIED is order-time 2 but permission bit 4.
    """

    GTC = 1
    DAY = 2
    SPECIFIED = 4
    SPECIFIED_DAY = 8


# --- retcodes -------------------------------------------------------------------------------


class Retcode(IntEnum):
    """The ``TRADE_RETCODE_*`` values this project reacts to by name."""

    REQUOTE = 10004
    REJECT = 10006
    PLACED = 10008
    DONE = 10009
    DONE_PARTIAL = 10010
    ERROR = 10011
    TIMEOUT = 10012
    INVALID = 10013
    INVALID_VOLUME = 10014
    INVALID_PRICE = 10015
    INVALID_STOPS = 10016
    TRADE_DISABLED = 10017
    MARKET_CLOSED = 10018
    NO_MONEY = 10019
    PRICE_CHANGED = 10020
    PRICE_OFF = 10021
    INVALID_EXPIRATION = 10022
    ORDER_CHANGED = 10023
    TOO_MANY_REQUESTS = 10024
    NO_CHANGES = 10025
    LOCKED = 10028
    FROZEN = 10029
    INVALID_FILL = 10030
    CONNECTION = 10031
    LIMIT_VOLUME = 10034
    INVALID_ORDER = 10035
    """"Incorrect or prohibited order type" -- a rejection of the request's ``type`` field,
    **not** the server saying a ticket names nothing. Easy to misread from the name alone, and
    expensive to: only requests that carry a type (DEAL, PENDING) can provoke it, so reading it
    as "already gone" would turn a refused open or close into a recorded success."""
    POSITION_CLOSED = 10036
    """"Position with the specified POSITION_IDENTIFIER has already been closed." This one
    genuinely does mean gone."""


class Outcome(StrEnum):
    """What to do about a result. Classifying beats treating every non-DONE as fatal, which
    abandons trades over a requote, and beats retrying everything, which hammers the server
    over a permanently invalid request."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partially filled"
    """Record the volume **actually** filled, never the volume requested."""
    GONE = "the target is gone"
    """The order or position being acted on does not exist any more.

    Not a failure. Closing, cancelling and modifying all describe a *target state*, and for a
    close or a cancel that state has been reached without us. For a modify there is nothing
    left to protect, so there is nothing left to get wrong either -- but it is a real
    divergence from the master, so the caller says so at WARNING rather than in passing."""
    REPRICE = "retry with a fresh price"
    REFILL = "retry with the alternative filling mode"
    BACKOFF = "server is busy; retry after a pause"
    CONNECTION = "connection problem; let the reconnect loop handle it"
    FATAL = "do not retry"


_OUTCOMES: dict[int, Outcome] = {
    Retcode.DONE: Outcome.SUCCEEDED,
    Retcode.PLACED: Outcome.SUCCEEDED,
    Retcode.DONE_PARTIAL: Outcome.PARTIAL,
    Retcode.REQUOTE: Outcome.REPRICE,
    Retcode.PRICE_CHANGED: Outcome.REPRICE,
    Retcode.PRICE_OFF: Outcome.REPRICE,
    Retcode.INVALID_FILL: Outcome.REFILL,
    Retcode.TOO_MANY_REQUESTS: Outcome.BACKOFF,
    Retcode.CONNECTION: Outcome.CONNECTION,
    Retcode.TIMEOUT: Outcome.CONNECTION,
    # Gone: we asked about something that is no longer there. Exactly one code means this.
    Retcode.POSITION_CLOSED: Outcome.GONE,
    # Fatal: the request is wrong, or the account cannot take it. Retrying changes nothing.
    Retcode.INVALID_ORDER: Outcome.FATAL,
    Retcode.NO_MONEY: Outcome.FATAL,
    Retcode.INVALID_VOLUME: Outcome.FATAL,
    Retcode.INVALID_PRICE: Outcome.FATAL,
    Retcode.INVALID_STOPS: Outcome.FATAL,
    Retcode.TRADE_DISABLED: Outcome.FATAL,
    Retcode.MARKET_CLOSED: Outcome.FATAL,
    Retcode.INVALID_EXPIRATION: Outcome.FATAL,
    Retcode.LIMIT_VOLUME: Outcome.FATAL,
    Retcode.LOCKED: Outcome.FATAL,
    Retcode.FROZEN: Outcome.FATAL,
    Retcode.REJECT: Outcome.FATAL,
    Retcode.INVALID: Outcome.FATAL,
    Retcode.ERROR: Outcome.FATAL,
    # ORDER_CHANGED reads like a success and is not one: the order moved on between our read
    # and our send, so the request was refused. Belief is written from the command rather than
    # read back from the terminal, so calling this a success would record a level the broker
    # never set, and the diff -- which compares the master against belief, never against the
    # slave -- would never look again. Silent divergence is the one failure with no recovery.
    Retcode.ORDER_CHANGED: Outcome.FATAL,
    # NO_CHANGES genuinely is one: the request was accepted and the levels already matched.
    # It is what makes a modification safe to retry.
    Retcode.NO_CHANGES: Outcome.SUCCEEDED,
}


PRICE_REFUSALS = frozenset(
    {Retcode.INVALID_PRICE, Retcode.PRICE_OFF, Retcode.REQUOTE, Retcode.PRICE_CHANGED}
)
"""Refusals about the request's *price* rather than its size, its stops or the account.

What the pending-order fallback keys on: a price the broker will not take as this resting
type may still be taken as the other one, or filled at market. ``INVALID_STOPS`` is
deliberately absent -- it is about the SL/TP distances, which neither flipping the type nor
going to market changes."""


def price_refused(retcode: int | None) -> bool:
    """Whether a refusal was about the request's price. See :data:`PRICE_REFUSALS`."""
    return retcode in PRICE_REFUSALS


def classify(retcode: int | None) -> Outcome:
    """Map a retcode to what to do next.

    ``None`` -- which ``order_send`` returns when the call itself failed rather than the trade
    -- is a connection problem, not a rejection.

    An unrecognised code is **fatal**, not retryable. A copy that has been retrying for
    seconds is no longer the trade the master took, so the bias has to be toward abandoning
    and surfacing rather than persisting into a worse fill.
    """
    if retcode is None:
        return Outcome.CONNECTION
    outcome = _OUTCOMES.get(int(retcode))
    if outcome is None:
        log.warning("unrecognised trade retcode %r; treating as fatal", retcode)
        return Outcome.FATAL
    return outcome


_TRANSIENT = frozenset({Outcome.CONNECTION, Outcome.BACKOFF, Outcome.REPRICE})
"""Outcomes that mean "the world was busy", as opposed to "the request was wrong".

``REFILL`` is deliberately absent: exhausting it means the symbol permits no filling mode we
have left to try, which does not improve by waiting.
"""


def is_transient(outcome: Outcome) -> bool:
    """Whether a failure with this outcome is worth deriving again later.

    Only the caller knows whether deriving it again is *safe* -- an entry goes stale, a partial
    close cannot be undone -- so this answers the narrower question of whether the failure was
    about the moment or about the request.
    """
    return outcome in _TRANSIENT


def describe_retcode(retcode: int | None) -> str:
    """Human-readable, for the log and the UI."""
    if retcode is None:
        return "no result (call failed)"
    try:
        return f"{Retcode(int(retcode)).name} ({int(retcode)})"
    except ValueError:
        return f"unknown retcode {retcode}"


# --- what both brokers share ----------------------------------------------------------------


def command_result(cmd: OrderCommand, terminal_id: str, *, ok: bool, **kw: Any) -> OrderResult:
    """The shell of every executor answer: identity copied from the command, the outcome
    defaulted from ``ok``. Both brokers' ``_fail``/``_ok`` delegate here, so a result can
    never claim to answer a command it does not."""
    kw.setdefault("outcome", (Outcome.SUCCEEDED if ok else Outcome.FATAL).value)
    # The pad travels on the command (set or scrubbed by _net_rr_target), so belief records
    # the target the broker is really resting at -- or forgets one it no longer is.
    kw.setdefault("padded_tp", cmd.padded_tp or None)
    return OrderResult(
        command_id=cmd.command_id,
        terminal_id=terminal_id,
        op=cmd.op,
        master_ticket=cmd.master_ticket,
        ok=ok,
        **kw,
    )


def backoff_delay(schedule: list[float], attempt: int) -> float:
    """Delay before retry ``attempt`` (1-based); the final entry repeats indefinitely."""
    if not schedule:
        return 5.0
    return schedule[min(attempt, len(schedule)) - 1]
