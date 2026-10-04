"""The day book: what the master did, and what became of it on each slave.

Display only, like :mod:`mt5copier.copy_stats`: nothing that decides what to send may read
it. The engine's action and result logs already hold every fact here, but one line at a time,
keyed per slave. A person asks the question the other way round -- "the master just opened
BTCUSD; did every slave follow, how big, how fast, and if not, why not?" -- so this groups a
tick's decisions under the master's trade and follows each one until the broker has answered.

The master's side is read from its own snapshot rather than from the actions, which describe
the *slave's* copy: a reversed slave's action carries the opposite direction and swapped
levels, and a close carries the slave's symbol name.

Nothing here is persisted. A restart starts an empty book, the same as the session statistics;
each outcome is handed to :attr:`DayBook.listener` as it settles, which is how
:mod:`mt5copier.history` keeps it.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .copy_stats import slippage_in_points
from .protocol import ActionKind, OrderResult, OrderType, PendingOrder, Position, PositionType
from .reconcile import Action, SkipReason, SlaveLink, invert_type

EVENT_HISTORY = 200
"""How many master events to keep. Bounded for the same reason as the action history."""

_ENTRY_KINDS = frozenset({ActionKind.OPEN, ActionKind.PLACE})
"""Events a slave misses for good if it is not being copied to when they happen: a slave
taken up again later is re-baselined, so the master's trade is then never copied to it."""

_ORDER_KINDS = frozenset(
    {ActionKind.PLACE, ActionKind.MODIFY_ORDER, ActionKind.CANCEL, ActionKind.ORDER_FILLED}
)


class Outcome(StrEnum):
    """Where one slave's copy of a master event stands."""

    SENDING = "sending"
    """Sent to the slave's worker; no answer yet."""
    DONE = "done"
    REFUSED = "refused"
    """The broker (or the executor) said no, for good."""
    FAILED = "failed"
    """A link problem rather than a refusal."""
    NO_ANSWER = "no answer"
    """Never answered; the copier gave up waiting."""
    SKIPPED = "skipped"
    """Deliberately not done, for the reason given."""
    FOLLOWS = "follows"
    """Nothing to send, because the slave's copy does this by itself (a resting order fills)."""
    DRY_RUN = "dry run"
    NOT_SENT = "not sent"
    """Meant to be sent, but could not be, for the reason given."""
    NOT_COPIED = "not copied"
    """The slave was held back or not connected when it happened, so it never will be."""

    @property
    def settled(self) -> bool:
        return self is not Outcome.SENDING

    @property
    def problem(self) -> bool:
        """Whether a person should look at it: the master did something a slave did not."""
        return self in (
            Outcome.REFUSED,
            Outcome.FAILED,
            Outcome.NO_ANSWER,
            Outcome.NOT_SENT,
            Outcome.NOT_COPIED,
        )


@dataclass(frozen=True, slots=True)
class SlaveOutcome:
    terminal_id: str
    outcome: Outcome
    reason: str | None = None
    lots: float | None = None
    """What the broker filled (or closed), never what was asked for."""
    latency_ms: float | None = None
    """From handing the command to the slave's worker to its answer arriving."""
    attempts: int = 0
    target_gone: bool = False
    """Answered ok, but the copy it acted on was already gone from the slave."""
    slippage_points: float | None = None
    """On a filled open: the fill against the master's entry, in the slave symbol's points.
    Positive is against us, as in :func:`~mt5copier.copy_stats.slippage_in_points`."""
    ts: float | None = None
    """Wall-clock time it was settled, or None while it is still waiting for an answer."""
    price: float | None = None
    """What the broker filled at, when it says."""


@dataclass(frozen=True, slots=True)
class MasterEvent:
    """One thing the master did, with what each slave made of it."""

    id: int
    ts: float
    """Wall-clock time the engine saw it."""
    kind: ActionKind
    preexisting: bool
    """The master already had this when a slave started being watched: a notice that it will
    not be copied there, not something the master just did."""
    master_ticket: int
    symbol: str
    label: str
    """``buy``/``sell`` for a position, ``buy limit`` and the like for a resting order."""
    volume: float
    """The master's volume after the event. For a partial close, what is left."""
    volume_before: float | None
    """For a partial close, what the master held before it, when that was seen."""
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    outcomes: tuple[SlaveOutcome, ...] = ()

    @property
    def problems(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome.problem)


@dataclass
class _Slot:
    """One slave's outcome while it can still change."""

    terminal_id: str
    outcome: Outcome
    reason: str | None = None
    lots: float | None = None
    latency_ms: float | None = None
    attempts: int = 0
    target_gone: bool = False
    slippage_points: float | None = None
    ts: float | None = None
    price: float | None = None
    sent_at: float | None = None
    master_ticket: int = 0
    direction: PositionType = PositionType.BUY
    entry: float = 0.0
    head: MasterEvent | None = None
    """The event it belongs to, for :attr:`DayBook.listener`."""
    action: Action | None = None

    def frozen(self) -> SlaveOutcome:
        return SlaveOutcome(
            self.terminal_id,
            self.outcome,
            self.reason,
            self.lots,
            self.latency_ms,
            self.attempts,
            self.target_gone,
            self.slippage_points,
            self.ts,
            self.price,
        )


@dataclass
class _Event:
    head: MasterEvent
    slots: dict[str, _Slot] = field(default_factory=dict)


_Master = Position | PendingOrder

Listener = Callable[[MasterEvent, SlaveOutcome, "Action | None"], None]
"""Told of each slave's outcome once it is settled: the event (its master side), the outcome,
and the slave's action when there was one."""


@dataclass
class DayBook:
    """Accumulates :class:`MasterEvent` records. Not thread-safe: the engine calls it under
    its own lock, like :class:`~mt5copier.copy_stats.StatsBook`."""

    _events: deque[_Event] = field(default_factory=lambda: deque(maxlen=EVENT_HISTORY))
    _ids: int = 0
    _pending: dict[int, _Slot] = field(default_factory=dict)
    """Command id -> the outcome its answer settles."""
    _positions: dict[int, Position] = field(default_factory=dict)
    _orders: dict[int, PendingOrder] = field(default_factory=dict)
    _prev_positions: dict[int, Position] = field(default_factory=dict)
    _prev_orders: dict[int, PendingOrder] = field(default_factory=dict)
    _tick: dict[tuple[int, ActionKind, bool], _Event] = field(default_factory=dict)
    _tick_ts: float = 0.0
    _since: float = field(default_factory=time.time)
    listener: Listener | None = None

    # --- recording --------------------------------------------------------------------------

    def begin_tick(
        self,
        ts: float,
        positions: Sequence[Position],
        orders: Sequence[PendingOrder],
    ) -> None:
        """Start one master tick. Its positions and orders, and the previous tick's, are what
        each event's master side is read from -- the previous ones for whatever this tick
        closed or cancelled, which is no longer in the master's lists."""
        self._prev_positions, self._prev_orders = self._positions, self._orders
        self._positions = {p.ticket: p for p in positions}
        self._orders = {o.ticket: o for o in orders}
        self._tick = {}
        self._tick_ts = ts

    def add(
        self,
        action: Action,
        outcome: Outcome,
        reason: str | None = None,
        *,
        invert: bool = False,
    ) -> None:
        """File one slave's action under this tick's event for its master trade.

        ``invert`` is whether that slave reverse-copies, needed only when the master's side
        has to be pieced together from the action itself.
        """
        preexisting = action.skip_reason is SkipReason.PREEXISTING
        if action.kind is ActionKind.ORDER_FILLED:
            outcome, reason = Outcome.FOLLOWS, None
        elif preexisting:
            reason = None
        key = (action.master_ticket, action.kind, preexisting)
        event = self._tick.get(key)
        if event is None:
            event = self._open_event(action, preexisting, invert)
            self._tick[key] = event
        slot = _Slot(
            action.terminal_id,
            outcome,
            reason,
            ts=None if outcome is Outcome.SENDING else self._tick_ts,
            master_ticket=action.master_ticket,
            direction=action.type,
            entry=action.entry,
            head=event.head,
            action=action,
        )
        event.slots[action.terminal_id] = slot
        if outcome.settled:
            self._tell(slot)

    def end_tick(self, links: Iterable[SlaveLink], holds: Mapping[str, str]) -> None:
        """Say which slaves an entry passed by: held back or not connected while the master
        opened or placed it. Without this a slave that is not there simply has no line, and a
        missing line reads the same as one the eye skipped."""
        for (_, kind, preexisting), event in self._tick.items():
            if kind not in _ENTRY_KINDS or preexisting:
                continue
            for link in links:
                tid = link.cfg.terminal_id
                if link.connected or tid in event.slots:
                    continue
                reason = holds.get(tid) or "it was not connected"
                slot = _Slot(
                    tid,
                    Outcome.NOT_COPIED,
                    reason,
                    ts=self._tick_ts,
                    master_ticket=event.head.master_ticket,
                    head=event.head,
                )
                event.slots[tid] = slot
                self._tell(slot)
        self._tick = {}

    def settle(self, action: Action, outcome: Outcome, reason: str) -> None:
        """Settle an outcome filed this tick as SENDING that was in the end not sent."""
        slot = self._tick_slot(action)
        if slot is not None and not slot.outcome.settled:
            slot.outcome, slot.reason, slot.ts = outcome, reason, self._tick_ts
            self._tell(slot)

    def on_sent(self, action: Action, command_id: int, *, sent_at: float) -> None:
        slot = self._tick_slot(action)
        if slot is not None:
            slot.sent_at = sent_at
            self._pending[command_id] = slot

    def on_result(self, result: OrderResult, *, now: float, ts: float) -> None:
        """An answer to the command the engine is waiting for. Late answers are not passed:
        the outcome they would settle is NO_ANSWER already, and the engine refuses them.

        ``now`` is the monotonic clock the send was timed on; ``ts`` the wall clock.
        """
        slot = self._pending.pop(result.command_id, None)
        if slot is None:
            return
        slot.ts = ts
        slot.attempts = result.attempts
        if slot.sent_at is not None:
            slot.latency_ms = max(0.0, (now - slot.sent_at) * 1000.0)
        if result.ok:
            slot.outcome = Outcome.DONE
            slot.lots = result.filled_volume
            slot.price = result.price or None
            slot.target_gone = result.target_gone
            if result.op is ActionKind.OPEN and result.price and result.point:
                slot.slippage_points = slippage_in_points(
                    slot.direction, slot.entry, result.price, result.point
                )
        else:
            slot.outcome = Outcome.FAILED if result.retryable else Outcome.REFUSED
            slot.reason = result.error or result.outcome or None
        self._tell(slot)

    def on_expired(self, terminal_id: str, master_ticket: int, *, ts: float) -> None:
        """The ledger gave up waiting on this slave and ticket. It does not say which op, so
        every command still waiting there is given up on, as the ledger's own expiry does."""
        for cid, slot in list(self._pending.items()):
            if slot.terminal_id == terminal_id and slot.master_ticket == master_ticket:
                slot.outcome = Outcome.NO_ANSWER
                slot.reason = "the slave never answered, so the copier gave up waiting"
                slot.ts = ts
                del self._pending[cid]
                self._tell(slot)

    def forget_sent_before(self, cutoff: float) -> None:
        """Drop commands too old to be answered, alongside the engine's own sweep."""
        for cid in [c for c, s in self._pending.items() if (s.sent_at or 0.0) < cutoff]:
            del self._pending[cid]

    def clear(self, *, ts: float) -> None:
        self._events.clear()
        self._pending.clear()
        self._tick = {}
        self._since = ts

    # --- reading ----------------------------------------------------------------------------

    @property
    def complete_since(self) -> float:
        """Wall-clock time from which every master event is still in the book: when it was
        started or cleared, or, once it is full, the oldest event it still holds."""
        return self._since

    def events(self, limit: int | None = None) -> list[MasterEvent]:
        """Most recent events, oldest first, as read-only copies."""
        items = list(self._events)
        if limit is not None:
            items = items[-limit:] if limit > 0 else []
        return [self._frozen(e) for e in items]

    # --- internals --------------------------------------------------------------------------

    def _tell(self, slot: _Slot) -> None:
        if self.listener is None or slot.head is None:
            return
        # The head as it stands: its outcomes are the listener's own to read off the slot.
        self.listener(slot.head, slot.frozen(), slot.action)

    def _tick_slot(self, action: Action) -> _Slot | None:
        key = (action.master_ticket, action.kind, action.skip_reason is SkipReason.PREEXISTING)
        event = self._tick.get(key)
        return event.slots.get(action.terminal_id) if event is not None else None

    def _open_event(self, action: Action, preexisting: bool, invert: bool) -> _Event:
        self._ids += 1
        master = self._master(action)
        before = self._prev_positions.get(action.master_ticket)
        if master is not None:
            label = _label(master)
            head = MasterEvent(
                id=self._ids,
                ts=self._tick_ts,
                kind=action.kind,
                preexisting=preexisting,
                master_ticket=action.master_ticket,
                symbol=master.symbol,
                label=label,
                volume=master.volume,
                volume_before=before.volume if before is not None else None,
                entry=master.price_open,
                sl=master.sl,
                tp=master.tp,
            )
        else:
            head = _from_action(self._ids, self._tick_ts, action, preexisting, invert)
        event = _Event(head)
        if len(self._events) == self._events.maxlen:
            self._since = self._events[1].head.ts
        self._events.append(event)
        return event

    def _master(self, action: Action) -> _Master | None:
        """The master's own record of the trade: this tick's, or else the last tick's."""
        ticket = action.master_ticket
        if action.kind in _ORDER_KINDS:
            # A filled order's ticket now names a position; the event is about the order.
            return self._orders.get(ticket) or self._prev_orders.get(ticket)
        return self._positions.get(ticket) or self._prev_positions.get(ticket)

    def _frozen(self, event: _Event) -> MasterEvent:
        return replace(event.head, outcomes=tuple(s.frozen() for s in event.slots.values()))


def _label(master: _Master) -> str:
    if isinstance(master, PendingOrder):
        return _order_label(master.type)
    return master.type.name.lower()


def _order_label(otype: OrderType) -> str:
    return otype.name.lower().replace("_", " ")


_UNINVERT_ORDER = {
    OrderType.BUY_LIMIT: OrderType.SELL_STOP,
    OrderType.SELL_STOP: OrderType.BUY_LIMIT,
    OrderType.SELL_LIMIT: OrderType.BUY_STOP,
    OrderType.BUY_STOP: OrderType.SELL_LIMIT,
}


def _from_action(
    event_id: int, ts: float, action: Action, preexisting: bool, invert: bool
) -> MasterEvent:
    """The master's side, pieced together from a slave's action when the master's own record
    was not seen -- for a slave that reverse-copies, by undoing the reversal."""
    direction = invert_type(action.type) if invert else action.type
    sl, tp = (action.tp, action.sl) if invert else (action.sl, action.tp)
    if action.order_type is not None:
        otype = (
            _UNINVERT_ORDER.get(action.order_type, action.order_type)
            if invert
            else (action.order_type)
        )
        label = _order_label(otype)
    else:
        label = direction.name.lower()
    return MasterEvent(
        id=event_id,
        ts=ts,
        kind=action.kind,
        preexisting=preexisting,
        master_ticket=action.master_ticket,
        symbol=action.master_symbol,
        label=label,
        volume=action.master_volume,
        volume_before=None,
        entry=action.entry,
        sl=sl,
        tp=tp,
    )


# --- wording ---------------------------------------------------------------------------------

_DONE_VERB = {
    ActionKind.OPEN: "opened",
    ActionKind.CLOSE: "closed",
    ActionKind.REDUCE: "closed",
    ActionKind.MODIFY_SLTP: "moved stop and target",
    ActionKind.PLACE: "placed",
    ActionKind.MODIFY_ORDER: "changed the order",
    ActionKind.CANCEL: "cancelled",
}

_LOTS_KINDS = frozenset({ActionKind.OPEN, ActionKind.CLOSE, ActionKind.REDUCE, ActionKind.PLACE})


def _price(value: float) -> str:
    return f"{value:.5f}".rstrip("0").rstrip(".") if value else "none"


def _num(value: float) -> str:
    return f"{value:g}"


def describe_master(event: MasterEvent) -> str:
    """What the master did, in a few words: ``opened BTCUSD buy 0.78 at 84,854``."""
    what = f"{event.symbol} {event.label}"
    vol = _num(event.volume)
    kind = event.kind
    if event.preexisting:
        state = "open" if kind is ActionKind.OPEN else "resting"
        return f"already had {what} {vol} {state} when a slave started watching it"
    if kind is ActionKind.OPEN:
        return f"opened {what} {vol} at {_price(event.entry)}"
    if kind is ActionKind.CLOSE:
        return f"closed {what} {vol}"
    if kind is ActionKind.REDUCE:
        if event.volume_before is not None:
            return f"closed part of {what}: {_num(event.volume_before)} to {vol}"
        return f"closed part of {what}, {vol} left"
    if kind is ActionKind.MODIFY_SLTP:
        return f"set stop {_price(event.sl)}, target {_price(event.tp)} on {what} {vol}"
    if kind is ActionKind.PLACE:
        return f"placed {what} {vol} at {_price(event.entry)}"
    if kind is ActionKind.MODIFY_ORDER:
        return (
            f"changed {what} {vol} to {_price(event.entry)}, "
            f"stop {_price(event.sl)}, target {_price(event.tp)}"
        )
    if kind is ActionKind.CANCEL:
        return f"cancelled {what} {vol}"
    if kind is ActionKind.ORDER_FILLED:
        return f"{what} {vol} filled at {_price(event.entry)}"
    return f"{kind.value} {what} {vol}"


def describe_master_full(event: MasterEvent) -> str:
    """:func:`describe_master` with the stop and target where the short form leaves them out."""
    short = describe_master(event)
    if event.kind in (ActionKind.OPEN, ActionKind.PLACE) and not event.preexisting:
        return f"{short}, stop {_price(event.sl)}, target {_price(event.tp)}"
    return short


def describe_outcome(kind: ActionKind, outcome: SlaveOutcome) -> str:
    """What became of it on one slave: ``opened 3.11 lots in 360 ms``."""
    o = outcome.outcome
    if o is Outcome.DONE:
        if outcome.target_gone:
            text = (
                "already gone from the slave"
                if kind in (ActionKind.CLOSE, ActionKind.CANCEL)
                else "not applied: the copy was already gone from the slave"
            )
        else:
            text = _DONE_VERB.get(kind, "done")
            if kind in _LOTS_KINDS and outcome.lots is not None:
                text += f" {_num(outcome.lots)} lots"
        if outcome.latency_ms is not None:
            text += f" in {outcome.latency_ms:,.0f} ms"
        if outcome.attempts > 1:
            text += f" ({outcome.attempts} tries)"
        return text
    if o is Outcome.SENDING:
        return "sending..."
    if o is Outcome.FOLLOWS:
        return "its own copy of the order filled with it"
    if o is Outcome.DRY_RUN:
        return "dry run: nothing sent"
    if o is Outcome.SKIPPED and outcome.reason is None:
        # A pre-existing trade: the master's line already says why.
        return "never copied: it was there before this slave was watched"
    reason = outcome.reason or "no reason given"
    return f"{o.value}: {reason}"
