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
from enum import StrEnum
from typing import Any, Protocol

from .config import NoStopPolicy, TerminalConfig
from .logging_setup import get_logger
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
    PARTIALS_DISABLED = "copy_partials is off for this slave"
    SLTP_SYNC_DISABLED = "copy_sltp is off for this slave"
    PENDING_DISABLED = "copy_pending is off for this slave"
    STOP_LIMIT_UNSUPPORTED = "stop-limit orders carry two prices and are not mirrored"
    NOTHING_TO_SEND = "the slave's own order fills by itself"
    PAUSED = "this slave is paused: it takes no new trades"


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

    sizing_basis: SizingBasis = SizingBasis.RISK_FROM_STOP
    """OPEN and PLACE: which calculation Stage 2b must use."""

    lot_multiple: float | None = None
    """OPEN with MASTER_LOT_MULTIPLE: the configured multiplier, so the log shows the concrete
    target lots rather than leaving the reader to look the setting up."""

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
    slave_ticket: int | None = None
    volume: float | None = None


# --- pure helpers ---------------------------------------------------------------------------


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
    return (tp, sl) if cfg.invert else (sl, tp)


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
) -> list[Action]:
    """Derive the intended actions for one slave. Pure and order-deterministic.

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

    # --- gone from the master -> close ------------------------------------------------------
    for ticket in sorted(copied_by_ticket):
        if ticket in master_by_ticket:
            continue
        stale = copied_by_ticket[ticket]
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
        ptype = invert_type(p.type) if cfg.invert else p.type
        slave_sl, slave_tp = effective_levels(p.sl, p.tp, cfg)
        c = copied_by_ticket.get(ticket)

        if c is None:
            if ticket in excluded:
                continue
            sizing = _choose_sizing(slave_sl, cfg)
            # A filter is the user's own explicit decision, so it is the more useful thing to
            # report when both apply.
            reason = _paused_reason(cfg) or _filter_reason(p.symbol, cfg) or sizing.skip_reason
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
                )
            )
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
                    skip_reason=None if cfg.copy_partials else SkipReason.PARTIALS_DISABLED,
                )
            )

        # SL/TP change, compared in slave-facing terms so an inverted copy is measured against
        # the levels it actually carries.
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
                    skip_reason=None if cfg.copy_sltp else SkipReason.SLTP_SYNC_DISABLED,
                    # Mirroring is mirroring: a master that clears its stop leaves the copy
                    # unprotected too, because the alternative is a copy that exits where the
                    # master does not. But it has to be *seen*, and it is easy to miss -- on
                    # a reversed slave it is the master removing its **target** that disarms
                    # the copy, since effective_levels has swapped the pair. Gated on the
                    # skip the same way the OPEN and PLACE branches are: an action that will
                    # not be sent disarms nothing.
                    unprotected=slave_sl == 0.0 and cfg.copy_sltp,
                )
            )

    return out


def reconcile_slave_orders(
    master_orders: Sequence[PendingOrder],
    master_position_tickets: Collection[int],
    copied: Sequence[CopiedOrder],
    cfg: TerminalConfig,
    excluded: Collection[int] = (),
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
        if ticket in resting:
            continue
        stale = copied_by_ticket[ticket]
        filled = ticket in position_tickets
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
        slave_sl, slave_tp = effective_levels(o.sl, o.tp, cfg)
        otype = invert_order_type(o.type) if cfg.invert else o.type
        c = copied_by_ticket.get(ticket)

        if c is None:
            if ticket in excluded:
                continue
            sizing = _choose_sizing(slave_sl, cfg)
            reason = (
                _paused_reason(cfg)
                or (None if cfg.copy_pending else SkipReason.PENDING_DISABLED)
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
                    sizing_basis=sizing.basis,
                    lot_multiple=sizing.lot_multiple,
                    skip_reason=reason,
                    unprotected=sizing.unprotected and reason is None,
                )
            )
            continue

        # A resting order can be moved -- entry price included, which no position can be. Its
        # volume cannot be modified in MT5 at all, so a volume change means a partial fill or a
        # delete-and-replace, neither of which is a modify; it is deliberately not compared.
        moved = not (
            _same(o.price_open, c.entry, _PRICE_EPS)
            and _same(slave_sl, c.synced_sl, _PRICE_EPS)
            and _same(slave_tp, c.synced_tp, _PRICE_EPS)
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
                    slave_ticket=c.slave_ticket,
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
            slave_ticket=slave_ticket,
            volume=slave_volume,
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
        self._last_ok: dict[str, float] = {}
        """Per slave: monotonic time of the last tick where the master and that slave were both
        observed connected. Staleness here is what triggers a re-baseline."""

    # --- state ---------------------------------------------------------------------------

    def believed(self, terminal_id: str | None = None) -> list[CopiedPosition]:
        return self._store.positions(terminal_id)

    def believed_orders(self, terminal_id: str | None = None) -> list[CopiedOrder]:
        return self._store.orders(terminal_id)

    def open_copies(self, terminal_id: str) -> list[OpenCopy]:
        """Every position and resting order believed to be on one slave, oldest master ticket
        first. Belief only: a command still in flight has no row yet."""
        copies = [
            OpenCopy(p.master_ticket, p.slave_ticket, p.slave_symbol, p.volume, resting=False)
            for p in self._store.positions(terminal_id)
        ]
        copies += [
            OpenCopy(o.master_ticket, o.slave_ticket, o.slave_symbol, o.volume, resting=True)
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
            )
            for action in self._emit(
                order_actions, fresh, simulate=simulate, dispatchable=dispatchable
            ):
                if action.kind is ActionKind.ORDER_FILLED:
                    self._on_fill(action, positions_by_ticket.get(action.master_ticket), link.cfg)
                elif action.kind is ActionKind.PLACE and not action.executable:
                    excluded_orders.add(action.master_ticket)

            for action in self._emit(
                reconcile_slave(master_positions, self.believed(tid), link.cfg, excluded=excluded),
                fresh,
                simulate=simulate,
                dispatchable=dispatchable,
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
            direction=invert_type(position.type) if cfg.invert else position.type,
            master_volume=position.volume,
            slave_ticket=order.slave_ticket,
            slave_volume=order.volume,
            synced_sl=order.synced_sl,
            synced_tp=order.synced_tp,
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
            if o.ticket in keep_orders and o.ticket not in already_orders
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

        if not result.ok:
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
            )
        elif result.op is ActionKind.PLACE:
            self._store.remember_order(
                terminal_id=last.terminal_id,
                master_ticket=last.master_ticket,
                slave_symbol=result.slave_symbol or last.slave_symbol,
                direction=last.type,
                order_type=last.order_type or OrderType.BUY_LIMIT,
                master_volume=last.master_volume,
                entry=last.entry,
                slave_ticket=result.slave_ticket,
                slave_volume=result.filled_volume,
                synced_sl=last.sl,
                synced_tp=last.tp,
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
            changes: dict[str, Any] = {"synced_sl": last.sl, "synced_tp": last.tp}
            if result.op is ActionKind.MODIFY_ORDER:
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
