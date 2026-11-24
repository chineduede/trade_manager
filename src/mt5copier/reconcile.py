"""Stage 2a: the diff engine -- decide what *would* be copied, and send nothing.

Everything in this module is pure. It takes the master's position list, what we believe
already exists on a slave, and that slave's config, and returns a list of intended
:class:`Action`s. No I/O, no ``MetaTrader5`` import, no clock -- so the entire decision layer
is exhaustively unit-testable without a terminal, a broker or a network.

**Reconcile state, don't consume events.** The master's positions are polled and diffed
against the last known state; opens, closes, volume changes and SL/TP changes are *derived*
from the difference. A state diff is self-healing: a missed tick, a reconnect or a process
restart still yields the correct target state on the next comparison, whereas an event stream
has to deliver every event exactly once or it silently diverges. Divergence in a copier means
real positions nobody is tracking.

Two things are deliberately **not** decided here, because they need the slave's own terminal to
answer and so belong to Stage 2b:

* **Lot sizing.** :attr:`Action.volume` is always ``None``: the arithmetic needs the slave's
  own terminal (``order_calc_profit``, and the symbol's volume step). What this module *does*
  decide is :class:`SizingBasis` -- whether the trade is sized from the slave's risk over the
  master's stop distance, sized as a multiple of the master's lots because there is no stop,
  or not copied at all. That choice depends only on pure data, so it is made here.
* **Symbol resolution.** :func:`map_symbol` honours an explicit ``symbol_map`` entry and the
  configured prefix/suffix, sharing that precedence with the real resolver. What it cannot do
  is the exact match or the base-name scan, both of which need the broker's own symbol list --
  so its answer is a best guess everywhere except the symbols the user mapped by hand.

:class:`CopyLedger` is the stateful half, kept here rather than in the engine so it is testable
in the same breath. It holds what we believe each slave has, behind a :class:`BeliefStore` --
scratch memory for a dry run, the SQLite mapping table when live. Swapping the store is the
*only* difference between rehearsal and the real thing, which is what makes the rehearsal worth
anything.

Belief follows outcomes, not intentions: :meth:`CopyLedger.step` proposes and
:meth:`CopyLedger.apply_result` believes. A dry run closes that loop itself by pretending every
order succeeded.
"""

from __future__ import annotations

import math
import time
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from .config import NoStopPolicy, TerminalConfig
from .logging_setup import get_logger
from .news import symbol_currencies
from .protocol import (
    ActionKind,
    OrderResult,
    OrderType,
    PendingOrder,
    Position,
    PositionType,
    SizingBasis,
)
from .symbols import configured_name

log = get_logger(__name__)

_VOLUME_EPS = 1e-6
"""Volumes come in steps of 0.01 at the finest, so this only absorbs float noise."""

_PRICE_EPS = 1e-8
"""Prices round-trip exactly from the terminal, but a broker may re-round a modified stop."""


def _same(a: float, b: float, eps: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=eps)


class SkipReason(StrEnum):
    """Why an otherwise-intended action would not be executed.

    An action carrying one of these is still *reported*, so the UI and the dry-run log show the
    decision instead of leaving the user to infer it from silence.
    """

    PREEXISTING = "already open before this slave was being watched"
    NO_STOP_LOSS = "no stop loss on master; cannot size"
    SYMBOL_BLACKLISTED = "symbol blacklisted for this slave"
    SYMBOL_NOT_WHITELISTED = "symbol not in this slave's whitelist"
    PARTIALS_DISABLED = "its slave manages this copy, so the master's partial close is not copied"
    SLTP_SYNC_DISABLED = (
        "its slave manages this copy, so the master's stop and target are not copied"
    )
    PENDING_DISABLED = "copy_pending is off for this slave"
    STOP_LIMIT_UNSUPPORTED = "stop-limit orders carry two prices and are not mirrored"
    NOTHING_TO_SEND = "the slave's own order fills by itself"
    PAUSED = "this slave is paused: it takes no new trades"
    GUARD_HOLDS = "a guard is holding this slave back: it takes no new trades"
    GUARD_NEEDS_STOP = "a guard requires a stop loss, and this trade has none"
    GUARD_RISK = "this slave risks more on a trade than a guard allows"
    GUARD_NEWS = "a guard takes no new trades in this symbol around high-impact news"
    BREAKEVEN_HOLDS = (
        "the copy is at breakeven, so its stop only tightens; a looser one is not copied"
    )


@dataclass(frozen=True, slots=True)
class Action:
    """One intended operation. Frozen and hashable, which is what lets the ledger tell a
    genuinely new decision from the same decision re-derived on the next tick."""

    kind: ActionKind
    terminal_id: str
    """The *slave* this action is for."""

    master_ticket: int
    master_symbol: str
    slave_symbol: str
    type: PositionType
    """Direction on the slave -- already inverted if the slave is set to reverse-copy."""

    master_volume: float
    """The master's volume *now*. For an OPEN this is what the copy is sized against."""

    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    slave_ticket: int | None = None
    volume: float | None = None
    """Always ``None`` in Stage 2a: lots are computed by Stage 2b against the slave's own
    balance and risk setting."""

    close_fraction: float | None = None
    """REDUCE only: the fraction of the slave position to close."""

    order_type: OrderType | None = None
    """Pending-order actions only: the resting order kind, already inverted if this slave
    reverse-copies. ``type`` stays the direction the order would become if it filled."""

    expiration_utc: int = 0
    """PLACE and MODIFY_ORDER: when the master's order expires, in real UTC seconds; 0 for
    none. Mirrored so the copy dies on the slave's own broker even if the copier is down at
    that moment, rather than only by the CANCEL the diff derives when the master's goes."""

    sizing_basis: SizingBasis = SizingBasis.RISK_FROM_STOP
    """OPEN and PLACE: which calculation Stage 2b must use."""

    lot_multiple: float | None = None
    """OPEN with MASTER_LOT_MULTIPLE: the configured multiplier, so the log shows the concrete
    target lots rather than leaving the reader to look the setting up."""

    master_managed: bool = True
    """OPEN and PLACE: whether the master will manage the copy, fixed on it once it opens. See
    :meth:`mt5copier.config.AppConfig.manages`."""

    risk_distance: float = 0.0
    """MODIFY_SLTP: the copy's latched one-R distance (see :attr:`CopiedPosition.risk_distance`),
    carried so a direct ticket's target can be re-padded from the risk the trade *opened* with
    rather than from wherever its stop sits now. 0 when the copy latched none."""

    padded_tp: float = 0.0
    """MODIFY_SLTP and MODIFY_ORDER: the padded target the copy already rests at, passed only
    while the ticket's own target (and, for an order, its entry) is unchanged -- the worker
    then re-sends it verbatim. See :attr:`CopiedPosition.padded_tp`."""

    skip_reason: SkipReason | None = None
    unprotected: bool = False
    """True when this would leave the slave holding a position with no stop loss -- either by
    opening one that way, or by taking the stop off one that had it. Copying an unstopped
    master trade sizes it but does not protect it, and mirroring a cleared stop disarms a copy
    that was protected a moment ago; neither should have to be inferred."""

    @property
    def executable(self) -> bool:
        return self.skip_reason is None

    def describe(self) -> str:
        """One plain line for the log and the UI. No markup -- callers add their own."""
        head = f"{self.kind.value.upper():<12} #{self.master_ticket} {self.slave_symbol}"
        label = self.order_type.name.lower() if self.order_type else self.type.name.lower()
        bits = [f"{head} {label}"]
        if self.kind in (ActionKind.OPEN, ActionKind.PLACE, ActionKind.MODIFY_ORDER):
            bits.append(f"@{self.entry:g}")
            bits.append(f"sl {self.sl:g}" if self.sl else "sl none")
            if self.tp:
                bits.append(f"tp {self.tp:g}")
            if self.expiration_utc:
                when = datetime.fromtimestamp(self.expiration_utc, tz=UTC)
                bits.append(f"expires {when:%Y-%m-%d %H:%M} UTC")
            if self.kind is not ActionKind.MODIFY_ORDER:
                if self.sizing_basis is SizingBasis.MASTER_LOT_MULTIPLE:
                    target = self.master_volume * (self.lot_multiple or 1.0)
                    bits.append(
                        f"size {self.lot_multiple:g} x master {self.master_volume:g}"
                        f" = {target:g} lots"
                    )
                else:
                    bits.append(f"master {self.master_volume:g} lots -> size: risk-based, stage 2b")
        elif self.kind is ActionKind.REDUCE:
            pct = (self.close_fraction or 0.0) * 100.0
            bits.append(f"close {pct:.1f}% (master now {self.master_volume:g})")
        elif self.kind is ActionKind.BOOK:
            pct = (self.close_fraction or 0.0) * 100.0
            bits.append(f"close {pct:g}% to book profit at breakeven")
        elif self.kind is ActionKind.MODIFY_SLTP:
            bits.append(f"sl {self.sl:g} tp {self.tp:g}")
        if self.unprotected:
            bits.append(
                "[UNPROTECTED: removes the slave's stop]"
                if self.kind is ActionKind.MODIFY_SLTP
                else "[UNPROTECTED: opens with no stop]"
            )
        if self.skip_reason is not None:
            bits.append(f"-- SKIPPED: {self.skip_reason.value}")
        return "  ".join(bits)


@dataclass(frozen=True, slots=True)
class CopiedPosition:
    """What we believe exists on one slave because of one master position.

    ``master_volume`` is the master volume this copy was *sized against*, not the master's
    current volume. Keeping it is what makes partial closes correct: a partial changes the
    volume ratio that every later action depends on, so the ratio has to be stored rather than
    recomputed from whatever the master happens to hold now.
    """

    terminal_id: str
    master_ticket: int
    slave_symbol: str
    type: PositionType
    master_volume: float

    synced_sl: float = 0.0
    synced_tp: float = 0.0
    """The stop and target **as the slave carries them**, which for a reversed copy are the
    master's swapped. Storing the slave-facing pair rather than the master's raw one is what
    lets the change detection compare like with like."""

    slave_ticket: int | None = None
    volume: float | None = None

    point: float = 0.0
    digits: int = 0
    """The slave symbol's point size and digits, from the fill: what the slippage buffer is
    counted in and what a breakeven stop is rounded to. 0 when unknown (a copy recorded
    before breakeven existed), which never triggers."""
    risk_distance: float = 0.0
    """The price distance from the slave's fill to the stop it opened with: one R. 0 when it
    opened with no stop, so an R trigger never fires on it."""
    price_value: float = 0.0
    """What a 1.0 price move is worth on one lot, from the fill; 0 when unknown. Turns the
    slave's commission into a breakeven distance."""
    master_managed: bool = True
    """Whether the master manages this copy: it follows the master's stop and target moves and
    partial closes, and its slave's own breakeven leaves it alone. Otherwise its slave manages
    it. Fixed when the copy opens, so a settings change reaches only trades opened after it."""
    commission_per_lot: float = 0.0
    """The round-turn commission per lot this copy pays, from its own opening deal: what that
    deal was charged, doubled for the way out, over the lots it filled. 0 when unknown -- a
    resting order's fill, a deal that showed none -- and the slave's setting is used instead.
    See :func:`round_turn_per_lot`."""
    padded_tp: float = 0.0
    """Direct trading only: the cost-padded target the copy actually rests at, written once
    when the ticket's target was first padded (:attr:`OrderCommand.padded_tp`). A stop move
    re-sends this exact level; it is cleared, and the pad computed afresh, when the ticket's
    own target changes. 0 for an unpadded copy."""
    breakeven_sl: float = 0.0
    """The breakeven floor, once triggered; 0 until then. The diff sends whichever of this
    and the master's stop is more protective -- see :func:`protective_sl`."""
    breakeven_exempt: bool = False
    """Set by hand on one copy: breakeven never triggers on it, whatever the slave's setting."""
    breakeven_at: float = 0.0
    """Set by hand on one copy: the price at which it goes to breakeven, in place of the
    slave's R trigger, and whether or not the slave has breakeven on. 0 is unset."""
    book_fraction: float = 0.0
    """A profit booking waiting to be sent: the fraction of the slave position to close,
    set when breakeven fires on a slave that books. 0 when there is none."""
    book_tries: int = 0
    """How many times the booking has been sent and failed."""
    booked: bool = False
    """The booking has gone out: sent and not yet answered, done, or given up. A breakeven
    that fires again (after a refused stop move) then books nothing more, and a restart while
    it is out does not send it again. Only a failure that is to be tried again clears it."""
    guard_close: bool = False
    """A guard closes this copy while the master's trade runs on: the diff sends a CLOSE and
    nothing else for it, and once it is closed the master's trade is never copied again here.
    Kept on disk, so a close not yet done is still owed after a restart."""


@dataclass(frozen=True, slots=True)
class CopiedOrder:
    """What we believe rests on one slave because of one master order.

    Separate from :class:`CopiedPosition` because the two diff differently: an order's entry
    price can move and a position's cannot, and an order can vanish for two quite different
    reasons -- cancelled, or filled.
    """

    terminal_id: str
    master_ticket: int
    slave_symbol: str
    order_type: OrderType
    master_volume: float
    entry: float = 0.0
    synced_sl: float = 0.0
    synced_tp: float = 0.0
    synced_expiration: int = 0
    """The expiry the copy was placed or last modified with, in real UTC seconds; 0 for
    none. Compared like the levels: the master changing its order's expiry is as much an
    order modification as moving its price."""
    slave_ticket: int | None = None
    volume: float | None = None
    point: float = 0.0
    digits: int = 0
    risk_distance: float = 0.0
    price_value: float = 0.0
    """Carried over to the position on a fill; see :class:`CopiedPosition`."""
    padded_tp: float = 0.0
    """Carried over to the position on a fill; see :class:`CopiedPosition`."""
    master_managed: bool = True
    """Carried over to the position on a fill. A resting order itself is always mirrored."""
    guard_close: bool = False
    """A guard cancels this order while the master's rests on; see
    :attr:`CopiedPosition.guard_close`."""


# --- pure helpers ---------------------------------------------------------------------------


def round_turn_per_lot(commission: float | None, lots: float | None) -> float:
    """A fill's round-turn commission per lot, from what its opening deal was charged: doubled,
    for every broker, on the reading that the way out costs what the way in did. A broker that
    charges the whole round turn on entry is then over-counted, which only puts a breakeven
    stop a little further out. 0 when there is nothing to go on."""
    if not commission or commission <= 0 or not lots or lots <= 0:
        return 0.0
    return 2.0 * commission / lots


def map_symbol(master_symbol: str, cfg: TerminalConfig) -> str:
    """This slave's best-guess name for a master symbol, with no connection to check against.

    An explicit ``symbol_map`` entry if there is one, else the configured prefix/suffix.
    Shares :func:`mt5copier.symbols.configured_name` with the executor rather than
    re-implementing the precedence, because two copies of that rule would eventually disagree
    and the disagreement would be a log naming one instrument while the order went to another.

    Still only an approximation: full resolution also has an exact match and a base-name scan,
    both of which need the broker's symbol list, so this cannot be right for a symbol the user
    has not mapped by hand. What it feeds is :attr:`Action.slave_symbol` -- the name in the log
    and the UI. Live, the ledger prefers the executor's answer once one arrives
    (``result.slave_symbol or last.slave_symbol``), so the guess is only durable in a **dry
    run**, whose beliefs live in their own store and are discarded on a mode change.
    """
    return configured_name(master_symbol, cfg.symbol_prefix, cfg.symbol_suffix, cfg.symbol_map)


def invert_type(ptype: PositionType) -> PositionType:
    return PositionType.SELL if ptype is PositionType.BUY else PositionType.BUY


_INVERTED_ORDER_TYPE = {
    OrderType.BUY_LIMIT: OrderType.SELL_STOP,
    OrderType.SELL_LIMIT: OrderType.BUY_STOP,
    OrderType.BUY_STOP: OrderType.SELL_LIMIT,
    OrderType.SELL_STOP: OrderType.BUY_LIMIT,
}
"""Reversing a resting order flips *both* the direction and the limit/stop kind.

The price level is what has to be preserved. A BUY_LIMIT rests **below** the market, so the
opposite trade at that same level is a sell triggered on the way down -- a SELL_STOP, not a
SELL_LIMIT, which would rest above the market and be rejected outright. Getting this wrong
does not produce a slightly-off copy; it produces an order the broker refuses or one that
fires on the wrong side.
"""


def invert_order_type(otype: OrderType) -> OrderType | None:
    """The reversed equivalent, or None if there isn't a sound one (stop-limit)."""
    return _INVERTED_ORDER_TYPE.get(otype)


def effective_levels(sl: float, tp: float, cfg: TerminalConfig) -> tuple[float, float]:
    """The (stop, target) a *slave* should carry, given the master's pair.

    For a normal copy they pass through. For a reversed copy they **swap**, and that is not
    cosmetic: the master's stop is the price at which its trade fails, which is precisely where
    the reversed trade succeeds. Passing the levels through unswapped would put the reversed
    position's stop on the profitable side of entry, where it would be hit immediately or
    rejected outright.

    The consequence to be aware of: a reversed copy's *risk* is measured to the master's
    take-profit. A master trade with a stop but no target therefore inverts into a copy with no
    stop at all, and falls to :class:`~mt5copier.config.NoStopPolicy` like any other unstopped
    trade -- so a slave set to both ``invert`` and ``SKIP`` will quietly copy nothing from a
    master that does not set targets. That is correct, but it is worth knowing.
    """
    return swap_levels(sl, tp, cfg.invert)


def swap_levels(sl: float, tp: float, inverted: bool) -> tuple[float, float]:
    """:func:`effective_levels` for a copy whose reversal is already known."""
    return (tp, sl) if inverted else (sl, tp)


def protective_sl(sl: float, floor: float, ptype: PositionType) -> float:
    """The more protective of two stops for a position of this direction; 0 is "no stop".

    Higher is more protective on a buy, lower on a sell. This is what keeps a breakeven from
    being undone: the master's stop is still followed whenever it is the tighter of the two
    (a trailing stop past entry), and ignored when it would loosen the copy or clear its stop.
    """
    if floor == 0.0:
        return sl
    if sl == 0.0:
        return floor
    return max(sl, floor) if ptype is PositionType.BUY else min(sl, floor)


@dataclass(frozen=True, slots=True)
class GuardGate:
    """What the slave's guards say about new trades, decided by the engine from its account.

    Only new trades: like a pause, a guard never stops the upkeep of what the slave holds --
    closes, partials and stop moves carry on, since cutting those off is what turns a loss
    limit into a bigger loss."""

    hold: bool = False
    """Take no new trades at all."""
    needs_stop: bool = False
    """Refuse a new trade that would carry no stop loss."""
    over_risk: bool = False
    """Refuse a new trade sized from its stop: the slave's risk budget, the same for every such
    trade, is over what a guard allows. A trade sized as a multiple of the master's lots has no
    known risk, so this cannot apply to it."""
    news: frozenset[str] = frozenset()
    """Refuse a new trade in a symbol that trades one of these currencies -- news is on for
    them. ``"*"`` refuses every new trade. A symbol whose currencies cannot be read from its
    name is refused while any news is on."""


def news_hits(news: frozenset[str], symbol: str) -> bool:
    if not news:
        return False
    if "*" in news:
        return True
    currencies = symbol_currencies(symbol)
    return currencies is None or bool(currencies & news)


def _guard_reason(
    gate: GuardGate | None, slave_sl: float, sizing: _SizingChoice, symbol: str
) -> SkipReason | None:
    if gate is None:
        return None
    if gate.hold:
        return SkipReason.GUARD_HOLDS
    if news_hits(gate.news, symbol):
        return SkipReason.GUARD_NEWS
    if gate.needs_stop and slave_sl == 0.0:
        return SkipReason.GUARD_NEEDS_STOP
    if gate.over_risk and sizing.basis is SizingBasis.RISK_FROM_STOP:
        return SkipReason.GUARD_RISK
    return None


def _paused_reason(cfg: TerminalConfig) -> SkipReason | None:
    """Checked first, and only for something new: a pause stops the slave *taking on* trades,
    never the upkeep of the ones it holds."""
    return SkipReason.PAUSED if cfg.paused else None


def _filter_reason(master_symbol: str, cfg: TerminalConfig) -> SkipReason | None:
    """Whitelist/blacklist are matched against the *master* symbol -- the name the user sees
    on the master when they type the filter, not the broker-mangled slave name."""
    sym = master_symbol.upper()
    if any(sym == entry.upper() for entry in cfg.symbol_blacklist):
        return SkipReason.SYMBOL_BLACKLISTED
    if cfg.symbol_whitelist and not any(sym == entry.upper() for entry in cfg.symbol_whitelist):
        return SkipReason.SYMBOL_NOT_WHITELISTED
    return None


def _preexisting_position_notice(pos: Position, cfg: TerminalConfig) -> Action:
    """A report, not an intention: this position exists and will never be copied."""
    slave_sl, slave_tp = effective_levels(pos.sl, pos.tp, cfg)
    return Action(
        kind=ActionKind.OPEN,
        terminal_id=cfg.terminal_id,
        master_ticket=pos.ticket,
        master_symbol=pos.symbol,
        slave_symbol=map_symbol(pos.symbol, cfg),
        type=invert_type(pos.type) if cfg.invert else pos.type,
        master_volume=pos.volume,
        entry=pos.price_open,
        sl=slave_sl,
        tp=slave_tp,
        skip_reason=SkipReason.PREEXISTING,
    )


def _preexisting_order_notice(order: PendingOrder, cfg: TerminalConfig) -> Action:
    """The same, for an order already resting before this slave was being watched."""
    slave_sl, slave_tp = effective_levels(order.sl, order.tp, cfg)
    otype = (invert_order_type(order.type) or order.type) if cfg.invert else order.type
    return Action(
        kind=ActionKind.PLACE,
        terminal_id=cfg.terminal_id,
        master_ticket=order.ticket,
        master_symbol=order.symbol,
        slave_symbol=map_symbol(order.symbol, cfg),
        type=otype.direction,
        order_type=otype,
        master_volume=order.volume,
        entry=order.price_open,
        sl=slave_sl,
        tp=slave_tp,
        skip_reason=SkipReason.PREEXISTING,
    )


@dataclass(frozen=True, slots=True)
class _SizingChoice:
    basis: SizingBasis
    lot_multiple: float | None = None
    skip_reason: SkipReason | None = None
    unprotected: bool = False


def _choose_sizing(slave_sl: float, cfg: TerminalConfig) -> _SizingChoice:
    """Decide how -- or whether -- this trade can be sized.

    Takes the stop **as the slave would carry it**, not the master's raw value, so a reversed
    copy is sized to the distance it actually risks. See :func:`effective_levels`.

    Risk sizing needs that distance, so a trade with no stop cannot be sized that way at all.
    That is routine rather than an edge case: discretionary traders attach stops after entry.

    With no stop, the master's own lot size is the only signal left, so ``LOT_MULTIPLE`` scales
    it. That is opt-in per slave and off by default, because it is the one path where the money
    at risk is neither the slave's configured risk nor knowable in advance -- an unstopped
    position has no defined worst case. The copy also opens unprotected, which the caller
    flags rather than leaving to be inferred.
    """
    if slave_sl != 0.0:
        return _SizingChoice(basis=SizingBasis.RISK_FROM_STOP)
    if cfg.no_stop_policy is NoStopPolicy.SKIP:
        return _SizingChoice(basis=SizingBasis.RISK_FROM_STOP, skip_reason=SkipReason.NO_STOP_LOSS)
    return _SizingChoice(
        basis=SizingBasis.MASTER_LOT_MULTIPLE,
        lot_multiple=cfg.no_stop_lot_multiple,
        unprotected=True,
    )


# --- the diff -------------------------------------------------------------------------------


def reconcile_slave(
    master_positions: Sequence[Position],
    copied: Sequence[CopiedPosition],
    cfg: TerminalConfig,
    excluded: Collection[int] = (),
    gate: GuardGate | None = None,
    master_manages: bool = True,
    master_order_tickets: Collection[int] = (),
) -> list[Action]:
    """Derive the intended actions for one slave. Pure and order-deterministic.

    ``master_manages`` is whether a copy opened now will be managed by the master; each copy
    already open carries its own answer.

    ``excluded`` holds master tickets this slave may never open -- positions that were already
    on the master when we started watching it. They are ignored silently here; the ledger
    announces them once, when it decides them. Closes, modifies and partials on an excluded
    ticket are impossible by construction: there is no copy of it to act on.

    Called with the master's *complete* position list. The caller must never pass a partial or
    unknown list: an empty list legitimately means "the master holds nothing", which produces a
    CLOSE for every copy, so a failed read has to be filtered out upstream rather than
    mistaken for a flat master.

    Closes are emitted before opens, so a log read top-to-bottom shows exits ahead of entries
    within a tick.
    """
    master_by_ticket = {p.ticket: p for p in master_positions}
    copied_by_ticket = {c.master_ticket: c for c in copied}
    out: list[Action] = []

    # --- gone from the master, or closed by a guard -> close ---------------------------------
    for ticket in sorted(copied_by_ticket):
        stale = copied_by_ticket[ticket]
        if ticket in master_by_ticket and not stale.guard_close:
            continue
        if ticket in master_order_tickets and not stale.guard_close:
            # A pending copy that fell back to market execution: the copy is already a
            # position while its master still *rests*. Nothing is wrong and nothing is
            # stale -- the master's order either fills, whereupon its position arrives
            # under this same ticket and the ordinary management above takes over, or is
            # cancelled, whereupon the ticket leaves this set too and the copy is closed
            # here on the next tick.
            continue
        out.append(
            Action(
                kind=ActionKind.CLOSE,
                terminal_id=cfg.terminal_id,
                master_ticket=ticket,
                master_symbol=stale.slave_symbol,
                slave_symbol=stale.slave_symbol,
                type=stale.type,
                master_volume=stale.master_volume,
                slave_ticket=stale.slave_ticket,
                volume=stale.volume,
            )
        )

    # --- present on the master --------------------------------------------------------------
    for ticket in sorted(master_by_ticket):
        p = master_by_ticket[ticket]
        slave_symbol = map_symbol(p.symbol, cfg)
        c = copied_by_ticket.get(ticket)
        # A copy stays the way it was opened: reversed if its direction is not the master's,
        # whatever ``invert`` says now. Read from the setting instead, flipping it under an open
        # copy would send the master's target as that copy's stop, on the wrong side of price.
        inverted = cfg.invert if c is None else c.type is not p.type
        ptype = invert_type(p.type) if inverted else p.type
        slave_sl, slave_tp = swap_levels(p.sl, p.tp, inverted)

        if c is None:
            if ticket in excluded:
                continue
            sizing = _choose_sizing(slave_sl, cfg)
            # A filter is the user's own explicit decision, so it is the more useful thing to
            # report when both apply.
            reason = (
                _paused_reason(cfg)
                or _guard_reason(gate, slave_sl, sizing, p.symbol)
                or _filter_reason(p.symbol, cfg)
                or sizing.skip_reason
            )
            out.append(
                Action(
                    kind=ActionKind.OPEN,
                    terminal_id=cfg.terminal_id,
                    master_ticket=ticket,
                    master_symbol=p.symbol,
                    slave_symbol=slave_symbol,
                    type=ptype,
                    master_volume=p.volume,
                    entry=p.price_open,
                    sl=slave_sl,
                    tp=slave_tp,
                    sizing_basis=sizing.basis,
                    lot_multiple=sizing.lot_multiple,
                    skip_reason=reason,
                    unprotected=sizing.unprotected and reason is None,
                    master_managed=master_manages,
                )
            )
            continue
        if c.guard_close:
            # A guard is closing it: the master's partials and stop moves no longer reach it.
            continue

        # Partial close. Measured against the volume this copy was sized against, not against
        # the master's previous tick, so a missed tick still lands on the right ratio.
        #
        # Only a decrease is a real event. An open position's volume cannot grow -- on a
        # hedging account a further entry is a new ticket, which arrives here as a new OPEN.
        # A growing volume therefore means an assumption is wrong (a netting master, most
        # likely), so the ledger warns rather than inventing a mirror for it, and the copy
        # stays anchored to the volume it was actually sized against.
        if p.volume < c.master_volume - _VOLUME_EPS:
            fraction = (c.master_volume - p.volume) / c.master_volume
            out.append(
                Action(
                    kind=ActionKind.REDUCE,
                    terminal_id=cfg.terminal_id,
                    master_ticket=ticket,
                    master_symbol=p.symbol,
                    slave_symbol=slave_symbol,
                    type=ptype,
                    master_volume=p.volume,
                    entry=p.price_open,
                    sl=slave_sl,
                    tp=slave_tp,
                    slave_ticket=c.slave_ticket,
                    close_fraction=fraction,
                    skip_reason=None if c.master_managed else SkipReason.PARTIALS_DISABLED,
                )
            )

        # Profit booked at breakeven: the engine set it on the copy when its breakeven fired,
        # and it stays until the booking is done or given up (see CopyLedger.apply_result).
        if c.book_fraction > 0.0 and not c.booked and c.slave_ticket is not None:
            out.append(
                Action(
                    kind=ActionKind.BOOK,
                    terminal_id=cfg.terminal_id,
                    master_ticket=ticket,
                    master_symbol=p.symbol,
                    slave_symbol=slave_symbol,
                    type=ptype,
                    master_volume=p.volume,
                    entry=p.price_open,
                    sl=slave_sl,
                    tp=slave_tp,
                    slave_ticket=c.slave_ticket,
                    close_fraction=c.book_fraction,
                )
            )

        # SL/TP change, compared in slave-facing terms so an inverted copy is measured against
        # the levels it actually carries.
        #
        # A breakeven floor applies whoever manages the copy: on one its slave manages it is
        # raised against the stop the copy already carries, and the target stays put. Only a
        # change that is *not* the floor's doing waits on the master managing the copy.
        follow = c.master_managed
        masters_sl = slave_sl
        if not follow and c.breakeven_sl != 0.0:
            floored = protective_sl(c.synced_sl, c.breakeven_sl, c.type)
            if not _same(floored, c.synced_sl, _PRICE_EPS):
                slave_sl, slave_tp, follow = floored, c.synced_tp, True
        else:
            slave_sl = protective_sl(slave_sl, c.breakeven_sl, c.type)
        if not (
            _same(slave_sl, c.synced_sl, _PRICE_EPS) and _same(slave_tp, c.synced_tp, _PRICE_EPS)
        ):
            out.append(
                Action(
                    kind=ActionKind.MODIFY_SLTP,
                    terminal_id=cfg.terminal_id,
                    master_ticket=ticket,
                    master_symbol=p.symbol,
                    slave_symbol=slave_symbol,
                    type=ptype,
                    master_volume=p.volume,
                    entry=p.price_open,
                    sl=slave_sl,
                    tp=slave_tp,
                    slave_ticket=c.slave_ticket,
                    risk_distance=c.risk_distance,
                    # The resting pad holds only while the ticket's target does: a target the
                    # person moved is a new intent, padded afresh by the worker.
                    padded_tp=c.padded_tp if _same(slave_tp, c.synced_tp, _PRICE_EPS) else 0.0,
                    skip_reason=None if follow else SkipReason.SLTP_SYNC_DISABLED,
                    # Mirroring is mirroring: a master that clears its stop leaves the copy
                    # unprotected too, because the alternative is a copy that exits where the
                    # master does not. But it has to be *seen*, and it is easy to miss -- on
                    # a reversed slave it is the master removing its **target** that disarms
                    # the copy, since effective_levels has swapped the pair. Gated on the
                    # skip the same way the OPEN and PLACE branches are: an action that will
                    # not be sent disarms nothing.
                    unprotected=slave_sl == 0.0 and follow,
                )
            )
        elif follow and not _same(masters_sl, slave_sl, _PRICE_EPS):
            # Nothing to send, but the master's stop is looser than the copy's breakeven and is
            # being ignored. Said once per level (the ledger drops a repeat), so the person can
            # see why the two terminals' stops differ. Right after a breakeven fires it is the
            # master's original stop, which doubles as confirmation that the floor is holding.
            out.append(
                Action(
                    kind=ActionKind.MODIFY_SLTP,
                    terminal_id=cfg.terminal_id,
                    master_ticket=ticket,
                    master_symbol=p.symbol,
                    slave_symbol=slave_symbol,
                    type=ptype,
                    master_volume=p.volume,
                    entry=p.price_open,
                    sl=masters_sl,
                    tp=slave_tp,
                    slave_ticket=c.slave_ticket,
                    skip_reason=SkipReason.BREAKEVEN_HOLDS,
                )
            )

    return out


def reconcile_slave_orders(
    master_orders: Sequence[PendingOrder],
    master_position_tickets: Collection[int],
    copied: Sequence[CopiedOrder],
    cfg: TerminalConfig,
    excluded: Collection[int] = (),
    gate: GuardGate | None = None,
    master_manages: bool = True,
) -> list[Action]:
    """Derive the intended pending-order actions for one slave. Pure, like its sibling.

    **Distinguishing a cancellation from a fill matters more than anything else here.** An
    order that leaves the master's list has either been deleted or been filled, and the right
    responses are opposites: cancel the slave's copy, or leave it alone because it is about to
    fill at the same price. Cancelling a copy that was going to fill silently drops a trade.

    The signal is the ticket. MT5 gives the position opened by a pending order *the order's own
    ticket*, so if the departed ticket now names a position, it filled. That is a documented
    MT5 behaviour rather than an inference from our own bookkeeping -- but it is the one
    assumption in this module that cannot be proven without a terminal, so it is worth
    confirming in a dry run before anything acts on it.
    """
    resting = {o.ticket: o for o in master_orders if o.type.is_pending}
    copied_by_ticket = {c.master_ticket: c for c in copied}
    position_tickets = set(master_position_tickets)
    out: list[Action] = []

    # --- gone from the master's order list --------------------------------------------------
    for ticket in sorted(copied_by_ticket):
        stale = copied_by_ticket[ticket]
        if ticket in resting and not stale.guard_close:
            continue
        # A guard cancelling the copy cancels it, whether the master's rests on or filled.
        filled = ticket in position_tickets and not stale.guard_close
        out.append(
            Action(
                kind=ActionKind.ORDER_FILLED if filled else ActionKind.CANCEL,
                terminal_id=cfg.terminal_id,
                master_ticket=ticket,
                master_symbol=stale.slave_symbol,
                slave_symbol=stale.slave_symbol,
                type=stale.order_type.direction,
                order_type=stale.order_type,
                master_volume=stale.master_volume,
                entry=stale.entry,
                slave_ticket=stale.slave_ticket,
                volume=stale.volume,
                skip_reason=SkipReason.NOTHING_TO_SEND if filled else None,
            )
        )

    # --- resting on the master --------------------------------------------------------------
    for ticket in sorted(resting):
        o = resting[ticket]
        slave_symbol = map_symbol(o.symbol, cfg)
        c = copied_by_ticket.get(ticket)
        # As for a position: a resting copy keeps the reversal it was placed with. Read off
        # the *direction* rather than the exact type, because the price fallback may have
        # placed the copy as the other resting type on the same side -- a buy limit resting
        # as a buy stop is not a reversal, a sell stop in its place is.
        inverted = cfg.invert if c is None else c.order_type.direction is not o.type.direction
        slave_sl, slave_tp = swap_levels(o.sl, o.tp, inverted)
        otype = invert_order_type(o.type) if inverted else o.type

        if c is None:
            if ticket in excluded:
                continue
            sizing = _choose_sizing(slave_sl, cfg)
            reason = (
                _paused_reason(cfg)
                or (None if cfg.copy_pending else SkipReason.PENDING_DISABLED)
                or _guard_reason(gate, slave_sl, sizing, o.symbol)
                or _filter_reason(o.symbol, cfg)
                # Rejected whether or not this slave inverts: a stop-limit carries an entry
                # *and* a trigger, and mirroring only one of them is worse than not mirroring.
                or (SkipReason.STOP_LIMIT_UNSUPPORTED if o.type.is_stop_limit else None)
                or sizing.skip_reason
            )
            out.append(
                Action(
                    kind=ActionKind.PLACE,
                    terminal_id=cfg.terminal_id,
                    master_ticket=ticket,
                    master_symbol=o.symbol,
                    slave_symbol=slave_symbol,
                    type=(otype or o.type).direction,
                    order_type=otype or o.type,
                    master_volume=o.volume,
                    entry=o.price_open,
                    sl=slave_sl,
                    tp=slave_tp,
                    expiration_utc=o.expiration_utc,
                    sizing_basis=sizing.basis,
                    lot_multiple=sizing.lot_multiple,
                    skip_reason=reason,
                    unprotected=sizing.unprotected and reason is None,
                    master_managed=master_manages,
                )
            )
            continue
        if c.guard_close:
            continue

        # A resting order can be moved -- entry price included, which no position can be. Its
        # volume cannot be modified in MT5 at all, so a volume change means a partial fill or a
        # delete-and-replace, neither of which is a modify; it is deliberately not compared.
        # Expiry is compared like the levels: the master changing when its order dies is an
        # order modification too.
        moved = not (
            _same(o.price_open, c.entry, _PRICE_EPS)
            and _same(slave_sl, c.synced_sl, _PRICE_EPS)
            and _same(slave_tp, c.synced_tp, _PRICE_EPS)
            and o.expiration_utc == c.synced_expiration
        )
        if moved:
            out.append(
                Action(
                    kind=ActionKind.MODIFY_ORDER,
                    terminal_id=cfg.terminal_id,
                    master_ticket=ticket,
                    master_symbol=o.symbol,
                    slave_symbol=slave_symbol,
                    type=(otype or o.type).direction,
                    order_type=otype or o.type,
                    master_volume=o.volume,
                    entry=o.price_open,
                    sl=slave_sl,
                    tp=slave_tp,
                    expiration_utc=o.expiration_utc,
                    slave_ticket=c.slave_ticket,
                    risk_distance=c.risk_distance,
                    # Entry compared too: an order's reward is measured from its entry, so a
                    # moved entry re-pads as much as a moved target does.
                    padded_tp=c.padded_tp
                    if _same(slave_tp, c.synced_tp, _PRICE_EPS)
                    and _same(o.price_open, c.entry, _PRICE_EPS)
                    else 0.0,
                    skip_reason=None if cfg.copy_pending else SkipReason.PENDING_DISABLED,
                )
            )

    return out


# --- the stateful half ----------------------------------------------------------------------

DEFAULT_GRACE_SECONDS = 5.0
"""Longest lapse in observation that still counts as continuous. See :meth:`CopyLedger.step`."""

RE_ARMABLE_KINDS = frozenset(
    {ActionKind.CLOSE, ActionKind.CANCEL, ActionKind.MODIFY_SLTP, ActionKind.MODIFY_ORDER}
)
"""Ops whose decision may safely be derived again after a transient failure.

Every one of them is idempotent against the terminal: closing a closed position, cancelling an
order that is already gone, and re-sending levels that already match are all no-ops the broker
reports as success. So asking twice costs nothing and asking once too few strands a copy.

Two kinds are deliberately absent. ``REDUCE`` closes a *fraction*, so if the first one did
reach the broker and only its answer was lost, deriving it again closes another slice -- and
over-closing cannot be undone. ``OPEN`` and ``PLACE`` are absent for a different reason: an
entry goes stale, and by the time a link returns the copy would be a late join.
"""

BOOK_RETRIES = 3
"""How many times a refused profit booking is tried again before it is given up."""

BOOK_RETRY_S = 5.0
"""How long after a failed booking the next try waits, so the retries are spread over a
little time rather than spent in consecutive polls on the same refusal."""

INFLIGHT_TIMEOUT_S = 30.0
"""How long to wait for a command's result before giving up on it. Generous, because the
executor's own retries can take two seconds and a busy terminal is slow, but finite: an entry
left in flight forever would freeze that ticket."""


class BeliefStore(Protocol):
    """Where the ledger keeps what it believes each slave holds.

    A protocol rather than a concrete type so the ledger does not import the SQLite store --
    which imports the ledger's own dataclasses -- and so dry run can use a scratch
    implementation with identical semantics. :class:`mt5copier.store.MappingStore` satisfies it.
    """

    def positions(self, terminal_id: str | None = None) -> list[CopiedPosition]: ...
    def orders(self, terminal_id: str | None = None) -> list[CopiedOrder]: ...
    def get_position(self, terminal_id: str, master_ticket: int) -> CopiedPosition | None: ...

    def remember_position(
        self,
        *,
        terminal_id: str,
        master_ticket: int,
        slave_symbol: str,
        direction: PositionType,
        master_volume: float,
        slave_ticket: int | None = None,
        slave_volume: float | None = None,
        synced_sl: float = 0.0,
        synced_tp: float = 0.0,
        point: float = 0.0,
        digits: int = 0,
        risk_distance: float = 0.0,
        price_value: float = 0.0,
        commission_per_lot: float = 0.0,
        padded_tp: float = 0.0,
        master_managed: bool = True,
    ) -> object: ...

    def remember_order(
        self,
        *,
        terminal_id: str,
        master_ticket: int,
        slave_symbol: str,
        direction: PositionType,
        order_type: OrderType,
        master_volume: float,
        entry: float = 0.0,
        slave_ticket: int | None = None,
        slave_volume: float | None = None,
        synced_sl: float = 0.0,
        synced_tp: float = 0.0,
        synced_expiration: int = 0,
        point: float = 0.0,
        digits: int = 0,
        risk_distance: float = 0.0,
        price_value: float = 0.0,
        padded_tp: float = 0.0,
        master_managed: bool = True,
    ) -> object: ...

    def update(self, terminal_id: str, master_ticket: int, **changes: object) -> object: ...
    def forget(self, terminal_id: str, master_ticket: int) -> None: ...


class _MemoryBeliefs:
    """Beliefs held in memory, for a dry run and for tests.

    Same semantics as the SQLite store, deliberately: the dry run is only a useful rehearsal if
    it exercises the code path live copying will use.
    """

    def __init__(self) -> None:
        self._positions: dict[tuple[str, int], CopiedPosition] = {}
        self._orders: dict[tuple[str, int], CopiedOrder] = {}

    def positions(self, terminal_id: str | None = None) -> list[CopiedPosition]:
        return [
            c
            for (tid, _), c in sorted(self._positions.items())
            if terminal_id is None or tid == terminal_id
        ]

    def orders(self, terminal_id: str | None = None) -> list[CopiedOrder]:
        return [
            c
            for (tid, _), c in sorted(self._orders.items())
            if terminal_id is None or tid == terminal_id
        ]

    def get_position(self, terminal_id: str, master_ticket: int) -> CopiedPosition | None:
        return self._positions.get((terminal_id, master_ticket))

    def remember_position(
        self,
        *,
        terminal_id: str,
        master_ticket: int,
        slave_symbol: str,
        direction: PositionType,
        master_volume: float,
        slave_ticket: int | None = None,
        slave_volume: float | None = None,
        synced_sl: float = 0.0,
        synced_tp: float = 0.0,
        point: float = 0.0,
        digits: int = 0,
        risk_distance: float = 0.0,
        price_value: float = 0.0,
        commission_per_lot: float = 0.0,
        padded_tp: float = 0.0,
        master_managed: bool = True,
    ) -> CopiedPosition:
        key = (terminal_id, master_ticket)
        self._orders.pop(key, None)
        copy = CopiedPosition(
            terminal_id=terminal_id,
            master_ticket=master_ticket,
            slave_symbol=slave_symbol,
            type=direction,
            master_volume=master_volume,
            synced_sl=synced_sl,
            synced_tp=synced_tp,
            slave_ticket=slave_ticket,
            volume=slave_volume,
            point=point,
            digits=digits,
            risk_distance=risk_distance,
            price_value=price_value,
            commission_per_lot=commission_per_lot,
            padded_tp=padded_tp,
            master_managed=master_managed,
        )
        self._positions[key] = copy
        return copy

    def remember_order(
        self,
        *,
        terminal_id: str,
        master_ticket: int,
        slave_symbol: str,
        direction: PositionType,
        order_type: OrderType,
        master_volume: float,
        entry: float = 0.0,
        slave_ticket: int | None = None,
        slave_volume: float | None = None,
        synced_sl: float = 0.0,
        synced_tp: float = 0.0,
        synced_expiration: int = 0,
        point: float = 0.0,
        digits: int = 0,
        risk_distance: float = 0.0,
        price_value: float = 0.0,
        padded_tp: float = 0.0,
        master_managed: bool = True,
    ) -> CopiedOrder:
        copy = CopiedOrder(
            terminal_id=terminal_id,
            master_ticket=master_ticket,
            slave_symbol=slave_symbol,
            order_type=order_type,
            master_volume=master_volume,
            entry=entry,
            synced_sl=synced_sl,
            synced_tp=synced_tp,
            synced_expiration=synced_expiration,
            slave_ticket=slave_ticket,
            volume=slave_volume,
            point=point,
            digits=digits,
            risk_distance=risk_distance,
            price_value=price_value,
            padded_tp=padded_tp,
            master_managed=master_managed,
        )
        self._orders[(terminal_id, master_ticket)] = copy
        return copy

    def update(
        self, terminal_id: str, master_ticket: int, **changes: object
    ) -> CopiedPosition | CopiedOrder | None:
        """Apply changes, translating the store's field names to the in-memory ones.

        The store calls the filled size ``slave_volume`` and these dataclasses call it
        ``volume``; anything the target does not have is dropped rather than raising, so a
        caller can pass one set of changes without knowing which kind it will land on.
        """
        key = (terminal_id, master_ticket)
        renamed = {("volume" if k == "slave_volume" else k): v for k, v in changes.items()}

        current_position = self._positions.get(key)
        if current_position is not None:
            allowed = {k: v for k, v in renamed.items() if k in current_position.__slots__}
            updated_position = replace(current_position, **allowed)  # type: ignore[arg-type]
            self._positions[key] = updated_position
            return updated_position

        current_order = self._orders.get(key)
        if current_order is not None:
            allowed = {k: v for k, v in renamed.items() if k in current_order.__slots__}
            updated_order = replace(current_order, **allowed)  # type: ignore[arg-type]
            self._orders[key] = updated_order
            return updated_order

        return None

    def forget(self, terminal_id: str, master_ticket: int) -> None:
        key = (terminal_id, master_ticket)
        self._positions.pop(key, None)
        self._orders.pop(key, None)


def _risk_distance(entry: float | None, sl: float) -> float:
    """One R for a copy: the distance from its entry to the stop it opened with, or 0."""
    if not entry or sl == 0.0:
        return 0.0
    return abs(entry - sl)


def _simulated_result(action: Action) -> OrderResult:
    """The outcome a dry run pretends it got: everything succeeded.

    The slave ticket is the master's, **negated**. It has to be something, because the code path
    that records belief expects one -- and a negative number is unmistakably synthetic, so if one
    ever turned up in a real trade request it would be obvious rather than plausible.
    """
    return OrderResult(
        command_id=0,
        terminal_id=action.terminal_id,
        op=action.kind,
        master_ticket=action.master_ticket,
        ok=True,
        outcome="simulated",
        slave_ticket=-action.master_ticket,
        slave_symbol=action.slave_symbol,
        requested_volume=action.volume,
        filled_volume=action.volume,
    )


@dataclass(frozen=True, slots=True)
class SlaveLink:
    """One slave and whether its terminal is reachable *right now*.

    The copier only ever acts on a slave whose link is up, so a slave with ``connected=False``
    produces no actions at all this tick -- not even a close. Nothing is lost by waiting:
    because the diff is derived from state rather than events, whatever the master did during
    the outage is still visible in the next comparison once the link returns.
    """

    cfg: TerminalConfig
    connected: bool = True
    gate: GuardGate | None = None
    """What its guards allow in new trades; None when it has none."""
    master_manages: bool = True
    """Whether the master manages the trades this slave opens now: see
    :meth:`mt5copier.config.AppConfig.manages`."""


@dataclass(frozen=True, slots=True)
class OpenCopy:
    """One copy a slave holds -- a position or a resting order -- described for a person.

    What the settings editor shows when a change would give the copies up, and what it checks
    again at the moment of giving them up: the master ticket is the key, because it is what
    belief is filed under.
    """

    master_ticket: int
    slave_ticket: int | None
    symbol: str
    volume: float | None
    resting: bool
    """A pending order rather than an open position."""
    breakeven_sl: float = 0.0
    breakeven_exempt: bool = False
    breakeven_at: float = 0.0
    """The copy's breakeven state, for the detail page. See :class:`CopiedPosition`."""
    price_value: float = 0.0
    point: float = 0.0
    """What a 1.0 price move is worth on one lot, and the symbol's point, from the fill; 0 when
    unknown. For the settings form's worked example."""
    risk_distance: float = 0.0
    """One R: the price distance from the copy's fill to the stop it opened with, 0 when it
    opened with none. Kept when the stop later moves, so R still means the risk taken."""


class CopyLedger:
    """Holds what we believe each slave has, and reports only decisions that are new.

    Two jobs, both needed for the Stage 2a exit criterion ("no spurious actions across
    restarts, reconnects and terminal restarts"):

    * **Simulate fills.** In dry-run nothing is sent, so nothing ever appears on a slave. Left
      alone, a state reconciler would therefore propose the same OPEN on every tick forever.
      Recording an executable OPEN as a believed position is what makes the diff converge, and
      it exercises the same bookkeeping Stage 2c will drive from SQLite.
    * **Suppress re-derivation.** Actions are frozen and compared by value, so an unchanged
      decision is silent, while a changed one is reported again. A decision *not* to open is
      the exception: it is recorded as an exclusion and never revisited, because reconsidering
      it later would be a late join under another name.

    Not thread-safe; the engine calls it from its drain thread only.
    """

    def __init__(self, store: BeliefStore | None = None) -> None:
        self._store: BeliefStore = store if store is not None else _MemoryBeliefs()
        """Where belief lives. In dry run this is a scratch store; live, it is the SQLite
        mapping table. Nothing else about the ledger changes between the two."""

        self._inflight: dict[tuple[str, int, ActionKind], float] = {}
        """Commands sent and not yet answered, with the monotonic time they went out.

        **This is what stops the same order being sent twice.** In dry run an action is
        believed the instant it is emitted, so re-derivation is naturally suppressed. Live,
        there is a gap of unknown length between sending an OPEN and learning that it filled --
        and during that gap the master's state is unchanged, so the diff would derive the very
        same OPEN again and send a second order. Anything in here is withheld until its result
        arrives or it times out.
        """
        self._last: dict[tuple[str, int, ActionKind], Action] = {}
        self._excluded: dict[str, set[int]] = {}
        """Per slave: master position tickets that may never be opened -- either they predate
        our watch, or we declined them once and a decline is final."""
        self._excluded_orders: dict[str, set[int]] = {}
        """The same, for resting orders. Kept separate from ``_excluded`` on purpose: declining
        to mirror an *order* says nothing about the position it may become. That position is a
        new event at a current price and is judged on its own -- which is what makes
        ``copy_pending = False`` behave sensibly, mirroring the fill but not the resting order.
        """
        self._warned_increase: set[tuple[str, int]] = set()
        self._book_wait: dict[tuple[str, int], float] = {}
        """Per copy: the monotonic time before which a failed booking is not tried again."""
        self._refused_breakevens: list[tuple[str, int]] = []
        """Copies whose breakeven move the broker refused, until the engine collects them to
        decide when to try again. See :meth:`take_refused_breakevens`."""
        self._last_ok: dict[str, float] = {}
        """Per slave: monotonic time of the last tick where the master and that slave were both
        observed connected. Staleness here is what triggers a re-baseline."""

    # --- state ---------------------------------------------------------------------------

    def believed(self, terminal_id: str | None = None) -> list[CopiedPosition]:
        return self._store.positions(terminal_id)

    def believed_orders(self, terminal_id: str | None = None) -> list[CopiedOrder]:
        return self._store.orders(terminal_id)

    def get_position(self, terminal_id: str, master_ticket: int) -> CopiedPosition | None:
        return self._store.get_position(terminal_id, master_ticket)

    def open_copies(self, terminal_id: str) -> list[OpenCopy]:
        """Every position and resting order believed to be on one slave, oldest master ticket
        first. Belief only: a command still in flight has no row yet."""
        copies = [
            OpenCopy(
                p.master_ticket,
                p.slave_ticket,
                p.slave_symbol,
                p.volume,
                resting=False,
                breakeven_sl=p.breakeven_sl,
                breakeven_exempt=p.breakeven_exempt,
                breakeven_at=p.breakeven_at,
                price_value=p.price_value,
                point=p.point,
                risk_distance=p.risk_distance,
            )
            for p in self._store.positions(terminal_id)
        ]
        copies += [
            OpenCopy(
                o.master_ticket,
                o.slave_ticket,
                o.slave_symbol,
                o.volume,
                resting=True,
                price_value=o.price_value,
                point=o.point,
            )
            for o in self._store.orders(terminal_id)
        ]
        return sorted(copies, key=lambda c: c.master_ticket)

    def abandon(self, terminal_id: str) -> list[OpenCopy]:
        """Stop tracking every copy on one slave, for good, and return what was let go.

        The copies stay open on the slave. What changes is that nothing here will close them,
        move their stops or mirror a partial close, so this is deliberate and the caller is
        expected to have said so first. Nothing is sent.

        **Forgetting the rows is not enough, and this is the part that is easy to get wrong.**
        The master still holds every trade it was copied from, and a master trade with no row
        and no exclusion reads as one that has just appeared -- so the next tick would copy it
        a second time, on top of the copy that was just let go. Each master ticket is therefore
        excluded, permanently, exactly as a declined trade is.

        A resting order is excluded twice, as an order and as a position. The slave's own order
        fills by itself, and MT5 gives the position it becomes the *order's* ticket, so when the
        master's order fills it would otherwise read as a brand-new position to copy -- doubling
        the exposure the orphaned order is about to create.

        In-flight commands are the caller's to rule out: a result that arrives for a copy that
        has already been let go would find no row to update.
        """
        let_go = self.open_copies(terminal_id)
        excluded = self._excluded.setdefault(terminal_id, set())
        excluded_orders = self._excluded_orders.setdefault(terminal_id, set())
        for copy in let_go:
            self._store.forget(terminal_id, copy.master_ticket)
            excluded.add(copy.master_ticket)
            if copy.resting:
                excluded_orders.add(copy.master_ticket)
        return let_go

    def set_breakeven(self, terminal_id: str, master_ticket: int, sl: float) -> None:
        """Record a breakeven floor on one copy. Nothing is sent from here: the next diff sees
        the floor and derives the ``MODIFY_SLTP``, so it goes through the same in-flight and
        result bookkeeping as any other stop move.

        The last reported stop move for the copy is forgotten, so the one this derives is
        always sent. Otherwise a floor set again at a level already tried -- after a refusal,
        or an exemption lifted -- would match that earlier action and be dropped in silence.
        """
        self._store.update(terminal_id, master_ticket, breakeven_sl=sl)
        self._last.pop((terminal_id, master_ticket, ActionKind.MODIFY_SLTP), None)

    def request_close(self, terminal_id: str, master_ticket: int) -> bool:
        """Close one copy -- cancel it, if it is resting -- whatever its master trade does.
        Nothing is sent from here: the next diff derives the ``CLOSE`` or ``CANCEL`` from the
        flag, which stays on disk until it is done. False if there is no such copy.

        What a guard calls when it overrides the master; see
        :attr:`CopiedPosition.guard_close`.
        """
        copy = self._store.update(terminal_id, master_ticket, guard_close=True)
        if copy is None:
            return False
        for kind in (ActionKind.CLOSE, ActionKind.CANCEL):
            self._last.pop((terminal_id, master_ticket, kind), None)
        return True

    def set_booking(self, terminal_id: str, master_ticket: int, fraction: float) -> None:
        """Book a fraction of one copy at its breakeven. Nothing is sent from here, as with the
        floor: the next diff derives the ``BOOK``. Ignored on a copy whose booking is already
        behind it or waiting, so a breakeven that fires again books nothing more."""
        copy = self._store.get_position(terminal_id, master_ticket)
        if copy is None or copy.booked or copy.book_fraction > 0.0:
            return
        self._store.update(terminal_id, master_ticket, book_fraction=fraction)
        self._last.pop((terminal_id, master_ticket, ActionKind.BOOK), None)

    def take_refused_breakevens(self) -> list[tuple[str, int]]:
        """The ``(slave, master ticket)`` pairs whose breakeven move was refused since the last
        call. Each has had its floor cleared, so the trigger will fire again; the caller
        decides how much further the price must go first."""
        taken, self._refused_breakevens = self._refused_breakevens, []
        return taken

    def _unarm_refused_breakeven(self, key: tuple[str, int, ActionKind]) -> bool:
        """If a refused stop move was a breakeven, clear the floor so it can be tried again.

        A broker refuses a stop too close to the price (``INVALID_STOPS``) and often does not
        say how close is too close: a floating-spread symbol can report no minimum at all and
        still enforce one. So a breakeven is not final the way a mirrored stop is. Its level
        does not change, but the price moves away from it, and the same request can succeed a
        moment later. Clearing the floor puts the copy back on the master's stop, which the
        broker already accepted, until the trigger fires again.
        """
        tid, ticket, _ = key
        last = self._last.get(key)
        copy = self._store.get_position(tid, ticket)
        if (
            last is None
            or copy is None
            or copy.breakeven_sl == 0.0
            or not _same(last.sl, copy.breakeven_sl, _PRICE_EPS)
        ):
            return False
        self._store.update(tid, ticket, breakeven_sl=0.0)
        self._last.pop(key, None)
        self._refused_breakevens.append((tid, ticket))
        return True

    def set_breakeven_exempt(self, terminal_id: str, master_ticket: int, exempt: bool) -> bool:
        """Exempt one copy from breakeven, or lift that. False if there is no such position.

        Exempting also drops a floor already set, and any breakeven price, so the copy follows
        the master's stop again from the next tick -- which may loosen it, and that is what was
        asked for. Lifting the exemption sets nothing: the trigger is checked afresh.
        """
        if self._store.get_position(terminal_id, master_ticket) is None:
            return False
        changes: dict[str, Any] = {"breakeven_exempt": exempt}
        if exempt:
            changes["breakeven_sl"] = 0.0
            changes["breakeven_at"] = 0.0
        self._store.update(terminal_id, master_ticket, **changes)
        return True

    def set_breakeven_at(self, terminal_id: str, master_ticket: int, price: float) -> bool:
        """Set one copy's breakeven price (0 clears it). False if there is no such position.

        Setting one lifts an exemption, since asking for a breakeven on a copy is the opposite
        of exempting it. A floor already set is left alone: the stop has already moved.
        """
        if self._store.get_position(terminal_id, master_ticket) is None:
            return False
        changes: dict[str, Any] = {"breakeven_at": price}
        if price:
            changes["breakeven_exempt"] = False
        self._store.update(terminal_id, master_ticket, **changes)
        return True

    def reset_session_state(self) -> None:
        """Forget everything that is *not* belief: in-flight commands, de-duplication memory,
        exclusions and continuity.

        Called when the backing store is swapped -- switching between dry run and live -- so
        simulated beliefs cannot leak into a live session or the reverse. Belief itself lives in
        the store and is swapped with it, not cleared here.
        """
        self._inflight.clear()
        self._last.clear()
        self._excluded.clear()
        self._excluded_orders.clear()
        self._warned_increase.clear()
        self._refused_breakevens.clear()
        self._last_ok.clear()

    # --- the tick ------------------------------------------------------------------------

    def step(
        self,
        master_positions: Sequence[Position],
        links: Iterable[SlaveLink],
        *,
        master_orders: Sequence[PendingOrder] = (),
        now: float | None = None,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        simulate: bool = True,
        dispatchable: frozenset[ActionKind] | None = None,
    ) -> list[Action]:
        """Reconcile every connected slave against the master's complete position and order
        lists.

        Returns only actions that are new or changed since the last call. Executable ones are
        folded into the believed state, which is what "dry run" means here: we act on our own
        books and never on a broker.

        **Continuity is what licenses an open.** A position may only be copied if we watched it
        appear -- i.e. if this slave and the master were both observed connected on the previous
        tick, no more than ``grace_seconds`` ago. Any longer lapse re-baselines that slave:
        everything currently open on the master becomes permanently excluded for it.

        One clock covers every way observation can lapse, because they are all the same event
        from here: the slave's link drops, the master's link drops (no tick runs at all), a
        terminal restarts, or the app was closed for a week. The gap is measured on our own
        monotonic clock rather than from ``Position.time``, which is *server* time -- brokers
        commonly run GMT+2/+3, so comparing it to a local epoch would be hours out.

        The grace window exists because a link blip of a few seconds is not a late join: a
        position opened four seconds ago has none of the sizing problem that makes adopting an
        old position dishonest, since the slave still fills within a whisker of the master's
        entry.

        ``dispatchable``, when given, names the action kinds the caller can actually send.
        Anything outside it is still derived and reported but never held as belief or put in
        flight, because nothing will answer for it. Default None means no restriction.
        """
        links = list(links)
        now = time.monotonic() if now is None else now
        slave_ids = {link.cfg.terminal_id for link in links}
        position_tickets = {p.ticket for p in master_positions}
        master_tickets = position_tickets | {o.ticket for o in master_orders}
        positions_by_ticket = {p.ticket: p for p in master_positions}
        fresh: list[Action] = []

        for link in links:
            tid = link.cfg.terminal_id
            if not link.connected:
                # Deliberately no actions, and no _last_ok update: the lapse accumulates, and
                # a long one costs this slave the positions opened during it.
                continue

            last_ok = self._last_ok.get(tid)
            if last_ok is None or (now - last_ok) > grace_seconds:
                fresh.extend(self._baseline(tid, master_positions, master_orders, link.cfg))

            excluded = self._excluded.setdefault(tid, set())
            excluded_orders = self._excluded_orders.setdefault(tid, set())
            self._warn_on_growth(tid, master_positions)

            # Orders first, so that a fill hands tracking over to the position *before* the
            # position diff runs. Otherwise the same trade would be seen as a brand-new
            # position and copied a second time.
            order_actions = reconcile_slave_orders(
                master_orders,
                position_tickets,
                self.believed_orders(tid),
                link.cfg,
                excluded=excluded_orders,
                gate=link.gate,
                master_manages=link.master_manages,
            )
            for action in self._emit(
                order_actions, fresh, simulate=simulate, dispatchable=dispatchable, now=now
            ):
                if action.kind is ActionKind.ORDER_FILLED:
                    self._on_fill(action, positions_by_ticket.get(action.master_ticket), link.cfg)
                elif action.kind is ActionKind.PLACE and not action.executable:
                    excluded_orders.add(action.master_ticket)

            for action in self._emit(
                reconcile_slave(
                    master_positions,
                    self.believed(tid),
                    link.cfg,
                    excluded=excluded,
                    gate=link.gate,
                    master_manages=link.master_manages,
                    # So a market-fallback copy -- a believed position whose master still
                    # rests -- is not read as "gone from the master" and closed.
                    master_order_tickets={o.ticket for o in master_orders if o.type.is_pending},
                ),
                fresh,
                simulate=simulate,
                dispatchable=dispatchable,
                now=now,
            ):
                if action.kind is ActionKind.OPEN and not action.executable:
                    # A decline is final. Whatever the reason -- no stop, a filter, or the
                    # position predating our watch -- we do not reconsider it later, because
                    # by then the slave would be filling far from the master's entry and the
                    # risk sizing would be a fiction. Attaching a stop five minutes on does
                    # not make the trade copyable; it makes it a late join.
                    excluded.add(action.master_ticket)

            self._last_ok[tid] = now

        self._prune(slave_ids, master_tickets)
        return fresh

    def _emit(
        self,
        actions: Sequence[Action],
        fresh: list[Action],
        *,
        simulate: bool,
        dispatchable: frozenset[ActionKind] | None = None,
        now: float | None = None,
    ) -> list[Action]:
        """Keep the actions that are new or changed, and hand them out once each.

        Two suppressions, doing different jobs. ``_last`` drops a decision identical to the one
        already reported -- the state has not moved, so neither has the answer. ``_inflight``
        drops one whose *command is still out*: the decision is current and correct, but acting
        on it again would duplicate an order that may already have filled.

        ``simulate`` is the whole difference between dry run and live. Simulating applies a
        successful outcome immediately, so the diff converges exactly as it will when real
        fills arrive; live leaves the action in flight until the broker answers.
        """
        kept: list[Action] = []
        for action in actions:
            key = (action.terminal_id, action.master_ticket, action.kind)
            if self._last.get(key) == action:
                continue
            if key in self._inflight:
                log.debug(
                    "withholding %s for #%s: still in flight", action.kind, action.master_ticket
                )
                continue
            wait = self._book_wait.get(key[:2]) if action.kind is ActionKind.BOOK else None
            if wait is not None and now is not None and now < wait:
                continue
            self._last[key] = action
            fresh.append(action)
            kept.append(action)
            if not action.executable:
                continue
            if dispatchable is not None and action.kind not in dispatchable:
                # Reported, but nothing can send it, so it must not become belief either. The
                # check precedes the simulate branch deliberately: a dry run that believed an
                # action had taken effect while live could not send it at all would stop being
                # a rehearsal of live, which is the only thing a dry run is for.
                continue
            if simulate:
                self.apply_result(_simulated_result(action), simulated=True)
            else:
                self._inflight[key] = time.monotonic()
                if action.kind is ActionKind.BOOK:
                    # Marked before the answer, so a restart while it is out does not send
                    # it again: an unanswered booking may have closed the slice already.
                    self._store.update(action.terminal_id, action.master_ticket, booked=True)
        return kept

    def _on_fill(self, action: Action, position: Position | None, cfg: TerminalConfig) -> None:
        """Hand tracking from a filled order to the position it became.

        The slave's own copy of the order fills by itself, so nothing is sent -- but the
        resulting position is ours, and its close and modifies must be mirrored. Converting
        here is also what stops the position diff from treating it as a brand-new trade and
        proposing a second copy.
        """
        order = next(
            (
                c
                for c in self._store.orders(action.terminal_id)
                if c.master_ticket == action.master_ticket
            ),
            None,
        )
        if position is None or order is None:
            return
        # The slave's order became the slave's position, keeping its ticket -- the same MT5
        # behaviour the fill was detected by. So the row changes kind in place rather than
        # being deleted and reinserted under a new identity.
        #
        # The levels carried over are the *order's*, not the master position's. They are the
        # same whenever the order's own sync kept up, and when it did not -- a MODIFY_ORDER
        # the broker refused -- they are the only record of what the slave actually rests on.
        # Adopting the master's here would write the drift away as already fixed, and since
        # the diff compares the master against belief and never against the slave, nothing
        # would ever look again. Keeping the truth makes the next tick derive a MODIFY_SLTP
        # and correct it.
        self._store.remember_position(
            terminal_id=action.terminal_id,
            master_ticket=action.master_ticket,
            slave_symbol=order.slave_symbol,
            # The order's own direction: it was placed reversed or not, whatever the setting
            # says now.
            direction=order.order_type.direction,
            master_volume=position.volume,
            slave_ticket=order.slave_ticket,
            slave_volume=order.volume,
            synced_sl=order.synced_sl,
            synced_tp=order.synced_tp,
            point=order.point,
            digits=order.digits,
            risk_distance=order.risk_distance,
            price_value=order.price_value,
            padded_tp=order.padded_tp,
            master_managed=order.master_managed,
        )

    def _warn_on_growth(self, terminal_id: str, master_positions: Sequence[Position]) -> None:
        """Complain, once, if a tracked master position's volume goes *up*.

        It should be unreachable: on a hedging account an open position can only be reduced,
        and a further entry arrives as a separate ticket. If it happens anyway the master is
        probably a netting account, which would break the partial-close ratios -- so this is
        worth a warning rather than a silent shrug, even though nothing acts on it.
        """
        by_ticket = {p.ticket: p for p in master_positions}
        for c in self.believed(terminal_id):
            p = by_ticket.get(c.master_ticket)
            if p is None or p.volume <= c.master_volume + _VOLUME_EPS:
                continue
            key = (terminal_id, c.master_ticket)
            if key in self._warned_increase:
                continue
            self._warned_increase.add(key)
            log.warning(
                "master position #%d grew from %g to %g lots, which should be impossible on a "
                "hedging account -- not mirrored; check whether this master is netting",
                c.master_ticket,
                c.master_volume,
                p.volume,
            )

    def _baseline(
        self,
        terminal_id: str,
        master_positions: Sequence[Position],
        master_orders: Sequence[PendingOrder],
        cfg: TerminalConfig,
    ) -> list[Action]:
        """Exclude everything the master already holds or has resting, and say so once.

        Announcing it matters: the alternative is a slave that sits there copying nothing while
        the master holds three positions, with no way to tell that from a broken copier.

        Anything we already believe we copied is *not* excluded -- it is ours, and its closes
        and modifies must still be mirrored.
        """
        believed_tickets = {c.master_ticket for c in self.believed(terminal_id)}
        already = self._excluded.get(terminal_id, set())
        keep = {p.ticket for p in master_positions if p.ticket not in believed_tickets}
        self._excluded[terminal_id] = keep

        believed_orders = {c.master_ticket for c in self.believed_orders(terminal_id)}
        already_orders = self._excluded_orders.get(terminal_id, set())
        keep_orders = {
            o.ticket for o in master_orders if o.type.is_pending and o.ticket not in believed_orders
        }
        self._excluded_orders[terminal_id] = keep_orders

        notices = [
            _preexisting_position_notice(p, cfg)
            for p in sorted(master_positions, key=lambda p: p.ticket)
            if p.ticket in keep and p.ticket not in already
        ]
        notices += [
            _preexisting_order_notice(o, cfg)
            for o in sorted(master_orders, key=lambda o: o.ticket)
            # An order ticket believed as a *position* is a market-fallback copy, not a
            # preexisting order: it stays excluded from placing (it is copied already) but
            # must not be announced as never-copied.
            if o.ticket in keep_orders
            and o.ticket not in already_orders
            and o.ticket not in believed_tickets
        ]
        return notices

    def apply_result(self, result: OrderResult, *, simulated: bool = False) -> None:
        """Fold the *outcome* of a command into belief.

        Belief follows what the broker did, not what we intended. That is the whole point of
        splitting this from :meth:`step`: an OPEN we sent and an OPEN that filled are different
        facts, and only the second one means a position exists.

        **A failed OPEN or PLACE is final**, and the ticket is excluded. The alternative --
        leaving it open to retry -- means a copy that failed for a permanent reason (no money,
        invalid stops) gets re-attempted every time the master nudges its stop, and a copy that
        failed for a transient one gets re-attempted long after the price has moved. Either way
        the trade being placed is no longer the trade the master took.

        **Everything else describes a target state rather than an entry**, and a target state
        does not go stale: a close is more urgent for being late, not less, and a stop should
        still follow the master an hour from now. So a *transient* failure on one of those --
        the link was down, the queue would not take it -- drops the de-duplication entry, and
        the next tick derives the decision again. A *refusal* is left to stand: re-deriving it
        would send the identical request into the identical refusal at every poll, which is
        the log rot ``DISPATCHABLE_KINDS`` exists to avoid. It stays surfaced loudly, once.

        **The limit of that, stated plainly:** a refusal is final for *that exact action*, not
        just for now. ``_emit`` de-duplicates on value equality, so if the master later comes
        back to a level pair the broker already refused -- a stop moved to breakeven, refused
        as too close, moved away, then moved to breakeven again -- the second attempt is
        dropped silently, even though the market may have moved enough for it to succeed. Any
        *other* level is sent normally. Fixing it properly means a per-key retry budget with
        backoff rather than a boolean, which is deliberately not in this stage; until then the
        one ERROR is the only notice, and ROADMAP.md lists it under Stage 3's known gaps.
        """
        key = (result.terminal_id, result.master_ticket, result.op)
        self._inflight.pop(key, None)

        if result.op is ActionKind.BOOK:
            self._on_booked(result, simulated=simulated)
            return

        if not result.ok:
            if (
                result.op is ActionKind.MODIFY_SLTP
                and not result.retryable
                and self._unarm_refused_breakeven(key)
            ):
                return
            if result.op in (ActionKind.OPEN, ActionKind.PLACE):
                bucket = self._excluded if result.op is ActionKind.OPEN else self._excluded_orders
                bucket.setdefault(result.terminal_id, set()).add(result.master_ticket)
            again = result.retryable and result.op in RE_ARMABLE_KINDS
            if again:
                self._last.pop(key, None)
            log.error(
                "%s for #%s on %s failed, %s: %s",
                result.op.value,
                result.master_ticket,
                result.terminal_id[:8],
                "and will be derived again" if again else "and will not be retried",
                result.error or result.outcome,
            )
            return

        last = self._last.get(key)
        if result.op in (ActionKind.CLOSE, ActionKind.CANCEL):
            self._store.forget(result.terminal_id, result.master_ticket)
            # Never copied again here: a guard's close leaves the master's trade running, and
            # without this the next diff would open it afresh. After a close the master made,
            # the ticket is gone from the master and pruned with the rest.
            bucket = self._excluded if result.op is ActionKind.CLOSE else self._excluded_orders
            bucket.setdefault(result.terminal_id, set()).add(result.master_ticket)
            return
        if last is None:
            # The action that produced this is gone from memory -- a restart between sending
            # and the answer, or an expiry that re-armed it. Belief cannot be reconstructed
            # from the result alone and inventing it would be worse than leaving it.
            #
            # What happens next differs by kind, so the message must not promise one of them
            # for the other: an OPEN or a PLACE created something nobody is tracking, which is
            # what startup reconciliation reports as an orphan, while a modification created
            # nothing and simply leaves belief where it was, so the diff derives it again.
            recovery = (
                "leaving it to startup reconciliation"
                if result.op in (ActionKind.OPEN, ActionKind.PLACE)
                else "belief is unchanged, so it will be derived again"
            )
            log.warning(
                "no pending action for %s #%s; %s",
                result.op.value,
                result.master_ticket,
                recovery,
            )
            return

        if result.op is ActionKind.OPEN:
            self._store.remember_position(
                terminal_id=last.terminal_id,
                master_ticket=last.master_ticket,
                slave_symbol=result.slave_symbol or last.slave_symbol,
                direction=last.type,
                master_volume=last.master_volume,
                slave_ticket=result.slave_ticket,
                slave_volume=result.filled_volume,
                synced_sl=last.sl,
                synced_tp=last.tp,
                point=result.point or 0.0,
                digits=result.digits or 0,
                risk_distance=_risk_distance(result.price, last.sl),
                price_value=result.price_value or 0.0,
                commission_per_lot=round_turn_per_lot(result.commission, result.filled_volume),
                padded_tp=result.padded_tp or 0.0,
                master_managed=last.master_managed,
            )
        elif result.op is ActionKind.PLACE and result.filled_at_market:
            # The pending could not rest and was executed at market instead, so what exists
            # on the slave is a position, and belief follows what the broker did. Keyed by
            # the master's *order* ticket, which is also the ticket of the position that
            # order opens if it fills -- the same MT5 behaviour the fill handover relies on
            # -- so the position diff takes over seamlessly then. Until then the diff holds
            # the copy open on the master's resting ticket (see ``reconcile_slave``), and
            # the order ticket is excluded so the order diff does not copy it a second time.
            self._store.remember_position(
                terminal_id=last.terminal_id,
                master_ticket=last.master_ticket,
                slave_symbol=result.slave_symbol or last.slave_symbol,
                direction=last.type,
                master_volume=last.master_volume,
                slave_ticket=result.slave_ticket,
                slave_volume=result.filled_volume,
                synced_sl=last.sl,
                synced_tp=last.tp,
                point=result.point or 0.0,
                digits=result.digits or 0,
                risk_distance=_risk_distance(result.price, last.sl),
                price_value=result.price_value or 0.0,
                commission_per_lot=round_turn_per_lot(result.commission, result.filled_volume),
                padded_tp=result.padded_tp or 0.0,
                master_managed=last.master_managed,
            )
            self._excluded_orders.setdefault(result.terminal_id, set()).add(result.master_ticket)
        elif result.op is ActionKind.PLACE:
            self._store.remember_order(
                terminal_id=last.terminal_id,
                master_ticket=last.master_ticket,
                slave_symbol=result.slave_symbol or last.slave_symbol,
                direction=last.type,
                # The type actually resting: the fallback may have flipped the master's into
                # the other resting type on the same side, and belief records the slave's own.
                order_type=result.placed_type or last.order_type or OrderType.BUY_LIMIT,
                master_volume=last.master_volume,
                entry=last.entry,
                slave_ticket=result.slave_ticket,
                slave_volume=result.filled_volume,
                synced_sl=last.sl,
                synced_tp=last.tp,
                synced_expiration=last.expiration_utc,
                point=result.point or 0.0,
                digits=result.digits or 0,
                # A resting order fills at its own price, give or take a stop order's slippage.
                risk_distance=_risk_distance(last.entry, last.sl),
                price_value=result.price_value or 0.0,
                padded_tp=result.padded_tp or 0.0,
                master_managed=last.master_managed,
            )
        elif result.op is ActionKind.REDUCE:
            # master_volume moves to what the master now holds, so the next partial is measured
            # against the right base. slave_volume is what is *left*, from the broker's own
            # figure where it gave one.
            current = self._store.get_position(last.terminal_id, last.master_ticket)
            remaining: float | None = None
            if current is not None and current.volume is not None:
                closed = result.filled_volume
                remaining = (
                    current.volume - closed
                    if closed is not None and not simulated
                    else current.volume * (1.0 - (last.close_fraction or 0.0))
                )
            self._store.update(
                last.terminal_id,
                last.master_ticket,
                master_volume=last.master_volume,
                slave_volume=remaining,
            )
        elif result.op in (ActionKind.MODIFY_SLTP, ActionKind.MODIFY_ORDER):
            if result.target_gone:
                # Reported ok because there was nothing left to get wrong, not because the
                # levels landed -- there was nothing to land them on. Writing them would
                # record a stop the slave never carried, and the diff never re-reads the
                # slave, so nothing would ever notice.
                return
            changes: dict[str, Any] = {
                "synced_sl": last.sl,
                "synced_tp": last.tp,
                # The pad the modify left resting: the one it re-sent or freshly computed,
                # or 0 when the target went out as the ticket's own.
                "padded_tp": result.padded_tp or 0.0,
            }
            if result.op is ActionKind.MODIFY_ORDER:
                changes["synced_expiration"] = last.expiration_utc
                if not self._is_order(last.terminal_id, last.master_ticket):
                    # It filled while the modify was in flight, and ``_on_fill`` has already
                    # converted the row, keeping the levels the slave's order really rested
                    # on. Both stores key on (terminal, master ticket) alone and would happily
                    # write an order's levels onto the position -- undoing that handover and
                    # recording levels the broker never applied to anything.
                    log.info(
                        "#%s filled while its move was in flight; leaving the position's "
                        "levels to the position diff",
                        last.master_ticket,
                    )
                    return
                changes["entry"] = last.entry
            self._store.update(last.terminal_id, last.master_ticket, **changes)

    def _on_booked(self, result: OrderResult, *, simulated: bool) -> None:
        """Fold a booking's outcome into its copy. Handled apart from every other op because
        it is the only one that is retried on a refusal, and only a few times.

        Done means done whatever else is true: a result arriving after a restart, with no
        action in memory, still marks the copy booked, since deriving it again would close a
        second slice. A slice too small to cut is final at once and only said; a refusal, or
        a transient failure, is tried again ``BOOK_RETRIES`` times, ``BOOK_RETRY_S`` apart,
        then given up.
        """
        tid, ticket = result.terminal_id, result.master_ticket
        key = (tid, ticket, ActionKind.BOOK)
        copy = self._store.get_position(tid, ticket)
        if copy is None:
            return
        name = f"#{ticket} on {tid[:8]}"
        if result.ok:
            self._book_wait.pop((tid, ticket), None)
            remaining = copy.volume
            if copy.volume is not None and not result.target_gone:
                closed = result.filled_volume
                remaining = (
                    copy.volume - closed
                    if closed is not None and not simulated
                    else copy.volume * (1.0 - copy.book_fraction)
                )
            self._store.update(tid, ticket, book_fraction=0.0, booked=True, slave_volume=remaining)
            return
        if result.unsplittable:
            self._store.update(tid, ticket, book_fraction=0.0, booked=True)
            log.warning(
                "%s: no profit booked at breakeven: %s. The position stays whole; only its "
                "stop moves",
                name,
                result.error,
            )
            return
        tries = copy.book_tries + 1
        if tries > BOOK_RETRIES:
            self._store.update(tid, ticket, book_fraction=0.0, booked=True, book_tries=tries)
            log.error(
                "%s: booking profit at breakeven failed %d times, the last: %s; given up, the "
                "position stays whole",
                name,
                tries,
                result.error or result.outcome,
            )
            return
        self._store.update(tid, ticket, book_tries=tries, booked=False)
        self._last.pop(key, None)
        self._book_wait[(tid, ticket)] = time.monotonic() + BOOK_RETRY_S
        log.warning(
            "%s: booking profit at breakeven failed (%s); trying again in %.0fs, retry %d of %d",
            name,
            result.error or result.outcome,
            BOOK_RETRY_S,
            tries,
            BOOK_RETRIES,
        )

    def _is_order(self, terminal_id: str, master_ticket: int) -> bool:
        """Whether that row is still a resting order rather than the position it became."""
        return any(c.master_ticket == master_ticket for c in self._store.orders(terminal_id))

    def expire_inflight(self, *, timeout_s: float = INFLIGHT_TIMEOUT_S) -> list[tuple[str, int]]:
        """Give up on commands that were never answered, and say which.

        A worker can die between receiving a command and reporting on it, and the order may or
        may not have reached the broker -- there is no way to tell from here. Leaving the entry
        in flight would freeze that ticket forever; clearing it silently could double-send.

        So: give up on it, exclude it, and say so at error level. If the order *did* go through,
        startup reconciliation will find the position as an orphan, which is exactly the
        mechanism for "something of ours exists that we cannot account for".
        """
        now = time.monotonic()
        lost: list[tuple[str, int]] = []
        for key, sent_at in list(self._inflight.items()):
            if (now - sent_at) <= timeout_s:
                continue
            tid, ticket, op = key
            del self._inflight[key]
            lost.append((tid, ticket))
            if op is ActionKind.OPEN:
                self._excluded.setdefault(tid, set()).add(ticket)
                consequence = (
                    "if it reached the broker it will appear as an orphan on the next "
                    "startup reconciliation"
                )
            elif op is ActionKind.PLACE:
                self._excluded_orders.setdefault(tid, set()).add(ticket)
                consequence = (
                    "if it reached the broker it will appear as an orphan on the next "
                    "startup reconciliation"
                )
            elif op is ActionKind.BOOK:
                # It may have closed the slice, and closing another cannot be undone, so the
                # booking is put behind the copy rather than tried again.
                self._store.update(tid, ticket, book_fraction=0.0, booked=True)
                consequence = (
                    "the profit may or may not have been booked; it is not tried again, "
                    "so check the slave's volume"
                )
            elif op in RE_ARMABLE_KINDS:
                # Nothing was created, so there is no orphan to find and the promise the
                # other branch makes would be a lie. What there is instead is a target state
                # that is still wanted, so re-arm it and let the next tick derive it again.
                self._last.pop(key, None)
                consequence = "it will be derived again on the next tick"
            else:
                # A REDUCE, which is the one op that is neither re-armable nor orphan-making:
                # it may have closed a slice already, and closing another cannot be undone.
                consequence = (
                    "the copy may already be smaller than our record; it is left alone "
                    "rather than reduced twice"
                )
            log.error(
                "%s for #%s on %s was never answered after %.0fs; giving up -- %s",
                op.value,
                ticket,
                tid[:8],
                timeout_s,
                consequence,
            )
        return lost

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    def inflight_on(self, terminal_id: str) -> int:
        """Commands sent to one slave and not yet answered.

        Belief follows outcomes, so a copy that is being opened is in no store yet, and a result
        that arrives for one already let go would find no row to update. Anything that gives a
        slave's copies up has to rule these out first.
        """
        return sum(1 for tid, _, _ in self._inflight if tid == terminal_id)

    def _prune(self, slave_ids: set[str], master_tickets: set[int]) -> None:
        """Drop session state for slaves that no longer have a role, and de-duplication memory
        for master tickets that are gone, so nothing grows without bound.

        **Belief is deliberately not pruned.** An earlier version deleted a slave's beliefs when
        it lost its role, which was harmless while they were simulated and is not harmless now:
        the rows describe *real open positions*, and dropping them would abandon them --
        untracked, unclosed, and indistinguishable from a stranger's trades next startup.
        Unassigning a slave stops the copier acting on it, which is all it should mean. If it is
        re-assigned, startup reconciliation matches the rows back up.
        """
        tracked = {(c.terminal_id, c.master_ticket) for c in self._store.positions()}
        tracked |= {(c.terminal_id, c.master_ticket) for c in self._store.orders()}
        for excluded in (self._excluded, self._excluded_orders):
            for tid in list(excluded):
                if tid not in slave_ids:
                    del excluded[tid]
                    continue
                # A ticket the master has closed or cancelled can never come back: MT5 tickets
                # are unique, so forgetting it keeps the set bounded by what actually exists.
                excluded[tid] &= master_tickets
        for tid in list(self._last_ok):
            if tid not in slave_ids:
                del self._last_ok[tid]
        for key in list(self._last):
            tid, ticket, _ = key
            if key in self._inflight:
                # Its result still has to find the action that produced it.
                continue
            if tid not in slave_ids or (
                ticket not in master_tickets and (tid, ticket) not in tracked
            ):
                del self._last[key]
        self._warned_increase = {
            (tid, ticket)
            for tid, ticket in self._warned_increase
            if tid in slave_ids and ticket in master_tickets
        }
        self._book_wait = {k: t for k, t in self._book_wait.items() if k in tracked}
