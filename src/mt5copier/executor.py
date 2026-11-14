"""Turning an :class:`~mt5copier.protocol.OrderCommand` into a real trade request.

**This is the only module in the project that causes an order to exist.** Everything upstream
decides; this acts. It runs inside a worker process, against that worker's own MT5 session,
and it talks to the terminal through a small injected ``session`` object so the whole thing is
testable without a terminal -- the tests drive it with a fake that returns scripted retcodes.

The order of operations matters and is deliberate:

1. **Refuse a live account.** Before anything else, because everything after it is harder to
   undo. Most of the terminals on this machine are prop or live.
2. **Resolve the symbol**, or refuse. A trade on the wrong instrument is worse than no trade.
3. **Size it**, or refuse. Below the symbol's minimum is a refusal, never a round-up.
4. **Send, classify, retry** -- bounded and fast. A copy still retrying after seconds is no
   longer the trade the master took, so abandoning and surfacing beats persisting into a
   worse fill.

Steps 2 and 3 belong to the operations that *create* something. Closing, cancelling and
modifying act on a ticket that already exists, so they skip straight from the guard to the
send -- and they share one further rule: **a target that is already gone counts as success**,
because each of them describes a state rather than a trade, and that state has been reached
without us. The rule only holds while "gone" and "we could not read it" stay distinguishable,
which is what :class:`ReadFailure` is for.
"""

from __future__ import annotations

import time
from dataclasses import replace
from enum import Enum
from typing import Any, Protocol

from .breakeven import stop_past_entry
from .execution import (
    OrderFilling,
    OrderTime,
    Outcome,
    Retcode,
    SymbolExpiration,
    TradeAction,
    alternative_filling,
    choose_filling,
    classify,
    is_transient,
    price_refused,
)
from .execution import describe_retcode as _describe
from .logging_setup import get_logger
from .protocol import (
    ACCOUNT_TRADE_MODE_DEMO,
    ActionKind,
    GuardNote,
    OrderCommand,
    OrderResult,
    PositionType,
    SizingBasis,
)
from .reconcile import round_turn_per_lot
from .sizing import (
    MarginFit,
    SizingFailure,
    SizingResult,
    SymbolFacts,
    fit_margin,
    fit_total_margin,
    lots_for_master_multiple,
    lots_for_risk,
    quantize_volume,
    widen_for_slippage,
)
from .symbols import SymbolCache

log = get_logger(__name__)

MAX_ATTEMPTS = 3
"""Hard cap on send attempts for one command."""

DEADLINE_S = 2.0
"""And a wall-clock cap, because three fast retries and three slow ones are different things.
Past this the trade is stale enough that not copying it is the better answer."""

BACKOFF_S = 0.15
"""Pause after TOO_MANY_REQUESTS. Short: the alternative to retrying is abandoning."""


class ReadFailure(Enum):
    """The one non-answer a terminal read is allowed to give.

    Deliberately distinct from ``None``, which means the terminal answered and the thing is
    not there. Everything that acts on an existing position or order treats "not there" as the
    desired state already being true, and reports it as a **success** -- so conflating the two
    would let a momentary read failure record a close that never happened, or a stop that
    never moved. That is the same ``None``-means-two-things trap MT5 sets with
    ``positions_get()``, one level up.
    """

    UNREADABLE = "unreadable"


UNREADABLE = ReadFailure.UNREADABLE
"""Shorthand for the only member, so call sites read ``if live is UNREADABLE``."""

LivePosition = tuple[str, PositionType, float]
"""``(symbol, direction, volume)`` for one open position, as the terminal reports it now."""


class Session(Protocol):
    """What the executor needs from a terminal. Implemented by the worker's ``_Session``."""

    def account_trade_mode(self) -> int | None: ...
    def server_offset_s(self) -> int | None: ...
    def symbol_names(self) -> list[str]: ...
    def symbol_facts(self, name: str) -> SymbolFacts | None: ...
    def tick(self, name: str) -> tuple[float, float] | None: ...
    def position(self, ticket: int) -> LivePosition | None | ReadFailure: ...
    def order_exists(self, ticket: int) -> bool: ...
    def loss_per_lot(
        self, direction: PositionType, symbol: str, entry: float, sl: float
    ) -> float | None: ...
    def order_send(self, request: dict[str, Any]) -> Any: ...
    def margin_for(
        self, direction: PositionType, symbol: str, volume: float, price: float
    ) -> float | None: ...
    def margin_used(self) -> float | None: ...
    def deal_commission(self, deal: int) -> float | None: ...
    def position_commission(self, ticket: int) -> tuple[float, float] | None: ...


class Executor:
    """Executes commands against one terminal. One per worker process."""

    def __init__(self, terminal_id: str, session: Session) -> None:
        self._terminal_id = terminal_id
        self._session = session
        self._symbols = SymbolCache()
        """Resolution is cached per (terminal, master symbol); the scan is the expensive part
        and a broker's symbol list does not change under a running terminal."""

    def forget_symbols(self) -> None:
        """Drop every cached symbol resolution for this terminal.

        The cached answers already re-resolve when a *setting* changes, but not when the
        *broker's* list does -- a symbol added, renamed or retired under a running terminal.
        Nothing can detect that from here, so the worker calls this on a reconnect and ``c``
        in the UI becomes a deliberate "re-read this broker".
        """
        self._symbols.forget(self._terminal_id)

    # --- the entry point ------------------------------------------------------------------

    def execute(self, cmd: OrderCommand) -> OrderResult:
        """Do one command. Never raises: a crash here must not take the worker down."""
        try:
            return self._execute(cmd)
        except Exception as exc:
            log.exception("executing %s for #%s crashed", cmd.op.value, cmd.master_ticket)
            return self._fail(cmd, error=f"executor crashed: {exc!r}")

    def _execute(self, cmd: OrderCommand) -> OrderResult:
        refusal = self._guard_live_account(cmd)
        if refusal is not None:
            return refusal

        if cmd.op in (ActionKind.CLOSE, ActionKind.REDUCE, ActionKind.BOOK):
            return self._close_or_reduce(cmd)
        if cmd.op is ActionKind.CANCEL:
            return self._cancel(cmd)
        if cmd.op is ActionKind.MODIFY_SLTP and cmd.to_breakeven:
            return self._to_breakeven(cmd)
        if cmd.op is ActionKind.MODIFY_SLTP:
            return self._modify_sltp(cmd)
        if cmd.op is ActionKind.MODIFY_ORDER:
            return self._modify_order(cmd)
        if cmd.op not in (ActionKind.OPEN, ActionKind.PLACE):
            return self._fail(cmd, error=f"nothing to send for {cmd.op.value}")

        resolved = self._symbols.resolve(
            self._terminal_id,
            cmd.master_symbol,
            self._session.symbol_names(),
            prefix=cmd.symbol_prefix,
            suffix=cmd.symbol_suffix,
            label=self._terminal_id[:8],
            symbol_map=dict(cmd.symbol_map),
        )
        if not resolved.ok or resolved.name is None:
            return self._fail(cmd, error=f"symbol {cmd.master_symbol!r} not found on this slave")
        symbol = resolved.name

        facts = self._session.symbol_facts(symbol)
        if facts is None:
            return self._fail(cmd, error=f"no symbol_info for {symbol!r}")

        sizing = self._size(cmd, symbol, facts)
        if not sizing.ok or sizing.lots is None:
            return self._fail(cmd, slave_symbol=symbol, error=sizing.describe())
        log.info("#%s on %s: %s", cmd.master_ticket, symbol, sizing.describe())

        lots = sizing.lots
        notes: tuple[GuardNote, ...] = ()
        for fit_to in (self._fit_margin, self._fit_total_margin):
            fit = fit_to(cmd, symbol, facts, lots)
            if fit is None:
                continue
            if fit.note is not None:
                notes += (fit.note,)
                log.warning("#%s on %s: %s", cmd.master_ticket, symbol, fit.note.text())
            if fit.lots is None:
                return self._fail(cmd, slave_symbol=symbol, error=fit.error, guard=notes)
            lots = fit.lots
        result = self._send_with_retries(cmd, symbol, facts, lots)
        return replace(result, guard=notes) if notes else result

    # --- a guard's margin cap -------------------------------------------------------------

    def _new_margin(self, cmd: OrderCommand, symbol: str, lots: float) -> float | None:
        """The margin ``lots`` of this new trade would take, asked of the terminal."""
        price = (
            cmd.entry if cmd.op is ActionKind.PLACE else self._market_price(symbol, cmd.direction)
        )
        return self._session.margin_for(cmd.direction, symbol, lots, price) if price else None

    def _fit_margin(
        self, cmd: OrderCommand, symbol: str, facts: SymbolFacts, lots: float
    ) -> MarginFit | None:
        """:func:`mt5copier.sizing.fit_margin`, with the margin asked of the terminal. None
        without the cap."""
        if cmd.max_margin is None:
            return None
        return fit_margin(
            lots=lots,
            margin=self._new_margin(cmd, symbol, lots),
            cap=cmd.max_margin,
            mode=cmd.margin_cap,
            symbol=symbol,
            facts=facts,
            rule=cmd.margin_rule,
            rule_id=cmd.margin_rule_id,
        )

    def _fit_total_margin(
        self, cmd: OrderCommand, symbol: str, facts: SymbolFacts, lots: float
    ) -> MarginFit | None:
        """:func:`mt5copier.sizing.fit_total_margin`, with the margin in use read now. None
        without the cap."""
        if cmd.max_total_margin is None:
            return None
        return fit_total_margin(
            lots=lots,
            margin=self._new_margin(cmd, symbol, lots),
            total=cmd.max_total_margin,
            in_use=self._session.margin_used(),
            mode=cmd.total_margin_cap,
            symbol=symbol,
            facts=facts,
            rule=cmd.total_margin_rule,
            rule_id=cmd.total_margin_rule_id,
        )

    # --- the guard ------------------------------------------------------------------------

    def _guard_live_account(self, cmd: OrderCommand) -> OrderResult | None:
        """Refuse to trade an account that is not a demo, unless explicitly overridden.

        First, and unconditional. Most of the terminals on this machine are prop or live, a
        role is one keypress away from being assigned to the wrong one, and the cost of the
        mistake is asymmetric in a way nothing downstream can correct.

        An unreadable trade mode is treated as *not* demo. "We could not tell" is not a reason
        to trade someone's funded account.
        """
        if cmd.allow_live:
            return None
        mode = self._session.account_trade_mode()
        if mode == ACCOUNT_TRADE_MODE_DEMO:
            return None
        where = "unreadable" if mode is None else f"trade_mode={mode}"
        log.error(
            "refusing to trade a non-demo account (%s); set allow_live_trading on this "
            "terminal if that is genuinely intended",
            where,
        )
        return self._fail(cmd, error=f"account is not a demo ({where}); refused")

    # --- closing, reducing, cancelling ------------------------------------------------------

    def _close_or_reduce(self, cmd: OrderCommand) -> OrderResult:
        """Close a copied position, in full or in part.

        The volume comes from the **terminal**, not from our record. Ours can be stale -- the
        user may have closed part of it by hand -- and a stale figure either gets rejected or,
        worse, closes the wrong amount. Reading it costs one call and removes the whole class
        of problem.

        A position that is already gone is reported as a *success*: the desired state is "not
        open", and it is not open. Treating it as a failure would leave the engine retrying a
        close against a position that no longer exists.
        """
        if cmd.slave_ticket is None:
            return self._fail(cmd, error="no slave ticket recorded for this copy")

        live = self._session.position(cmd.slave_ticket)
        if live is UNREADABLE:
            # "We could not tell" is not "it is gone". Saying gone here would report a close
            # that never happened, and the ledger would drop the row for a position that is
            # still open. Fail it as transient instead, and the next tick derives it again.
            return self._fail(
                cmd,
                retryable=True,
                error=f"could not read slave position {cmd.slave_ticket} to close it",
            )
        if live is None:
            if cmd.op is ActionKind.CLOSE and self._session.order_exists(cmd.slave_ticket):
                # A flipped pending copy can still be resting when its master's order fills,
                # after which belief calls it a position and mirrors the master's exit as a
                # CLOSE. "Close" means "be flat", and for a ticket still resting that is a
                # cancellation -- declaring it gone instead would orphan a live order.
                log.info(
                    "#%s: slave ticket %s is still a resting order; cancelling it instead "
                    "of closing",
                    cmd.master_ticket,
                    cmd.slave_ticket,
                )
                return self._cancel(cmd)
            log.info("#%s: slave position %s already gone", cmd.master_ticket, cmd.slave_ticket)
            return self._ok(
                cmd,
                slave_ticket=cmd.slave_ticket,
                filled_volume=0.0,
                outcome="already closed",
            )
        symbol, direction, open_volume = live

        volume = open_volume
        if cmd.op in (ActionKind.REDUCE, ActionKind.BOOK):
            fraction = cmd.close_fraction or 0.0
            if not 0.0 < fraction < 1.0:
                return self._fail(cmd, error=f"nonsensical close fraction {fraction!r}")
            facts = self._session.symbol_facts(symbol)
            if facts is None:
                return self._fail(
                    cmd,
                    slave_symbol=symbol,
                    retryable=True,
                    error=f"no symbol_info for {symbol!r}",
                )
            sized = quantize_volume(open_volume * fraction, facts)
            if cmd.op is ActionKind.BOOK:
                # Booking is the copier's own choice, so it gives way: a slice it cannot cut,
                # or one that would leave less than the broker lets stay open, is not booked
                # at all -- never rounded up into closing the whole trade.
                left = open_volume - (sized.lots or 0.0)
                if not sized.ok or sized.lots is None or left < facts.volume_min - 1e-9:
                    words = (
                        f"{fraction:.0%} of {open_volume:g} lots"
                        + (f" would leave {left:g}" if sized.ok else f": {sized.describe()}")
                        + f"; the minimum on {symbol} is {facts.volume_min:g}"
                    )
                    return self._fail(
                        cmd, slave_symbol=symbol, unsplittable=True, error=f"cannot book {words}"
                    )
            if not sized.ok or sized.lots is None:
                # Too small a slice to express. Leaving the position whole is the safe
                # answer: over-closing cannot be undone.
                return self._fail(
                    cmd, slave_symbol=symbol, error=f"partial close {sized.describe()}"
                )
            volume = min(sized.lots, open_volume)

        # Closing is the opposite side of the position, at the opposite side of the book.
        closing_direction = PositionType.SELL if direction is PositionType.BUY else PositionType.BUY
        price = self._market_price(symbol, closing_direction)
        if price is None:
            return self._fail(
                cmd, slave_symbol=symbol, retryable=True, error=f"no tick for {symbol!r}"
            )

        facts = self._session.symbol_facts(symbol)
        mask = facts.filling_mask if facts is not None else 0
        request = {
            "action": int(TradeAction.DEAL),
            "position": cmd.slave_ticket,
            "symbol": symbol,
            "volume": volume,
            "type": 1 if closing_direction is PositionType.SELL else 0,
            "price": price,
            "deviation": cmd.deviation_points,
            "magic": cmd.magic,
            "comment": cmd.comment[:31],
            "type_filling": int(choose_filling(mask, is_pending=False)),
            "type_time": 0,
        }
        return self._send(cmd, request, symbol, volume, mask, reprice_direction=closing_direction)

    def _cancel(self, cmd: OrderCommand) -> OrderResult:
        """Delete a copied pending order.

        Like a close, an order that is already gone counts as success -- it may have been
        filled or removed since the command was issued, and either way it is not resting.
        """
        if cmd.slave_ticket is None:
            return self._fail(cmd, error="no slave ticket recorded for this copy")
        if not self._session.order_exists(cmd.slave_ticket):
            log.info("#%s: slave order %s already gone", cmd.master_ticket, cmd.slave_ticket)
            return self._ok(cmd, slave_ticket=cmd.slave_ticket, outcome="already gone")

        request = {"action": int(TradeAction.REMOVE), "order": cmd.slave_ticket}
        result = self._session.order_send(request)
        retcode = getattr(result, "retcode", None) if result is not None else None
        outcome = classify(retcode)
        if outcome in (Outcome.SUCCEEDED, Outcome.GONE):
            # GONE means the server says the ticket names nothing -- it filled or went while
            # the command was in the queue. Not resting is what we asked for.
            return self._ok(cmd, slave_ticket=cmd.slave_ticket, outcome=outcome.value, attempts=1)
        return self._fail(
            cmd,
            retcode=retcode,
            attempts=1,
            retryable=is_transient(outcome),
            error=f"{_describe(retcode)} cancelling order {cmd.slave_ticket}",
        )

    # --- modifying ---------------------------------------------------------------------------

    def _modify_sltp(self, cmd: OrderCommand) -> OrderResult:
        """Move a copied position's stop and target to match the master's.

        ``TRADE_ACTION_SLTP`` takes five fields and ignores the rest, and it **overwrites both
        levels every time** -- the request is a zero-filled struct, so omitting ``sl`` and
        sending ``sl=0.0`` are the same bytes and both mean "no stop". There is no way to
        change one and leave the other. That is exactly what mirroring wants: the master's
        pair goes on the wire whole, and a master clearing its stop is mirrored by the zero
        falling out of the same path rather than needing a case of its own.

        The symbol is read from the **terminal**, for the same reason the close volume is: our
        record is the master's spelling put through a prefix/suffix, while the position knows
        what it actually is.
        """
        if cmd.slave_ticket is None:
            return self._fail(cmd, error="no slave ticket recorded for this copy")

        live = self._session.position(cmd.slave_ticket)
        if live is UNREADABLE:
            return self._fail(
                cmd,
                retryable=True,
                error=f"could not read slave position {cmd.slave_ticket} to modify it",
            )
        if live is None:
            # The copy is gone while the master still holds the trade, so there is nothing
            # left to protect. Loud, because it is a real divergence: from here the master's
            # exit will close nothing.
            log.warning(
                "#%s: slave position %s is gone, so its stops cannot follow the master",
                cmd.master_ticket,
                cmd.slave_ticket,
            )
            return self._ok(
                cmd,
                slave_ticket=cmd.slave_ticket,
                outcome=Outcome.GONE.value,
                target_gone=True,
                attempts=0,
            )
        symbol = live[0]

        request = {
            "action": int(TradeAction.SLTP),
            "symbol": symbol,
            "position": cmd.slave_ticket,
            # Coerced explicitly: order_send rejects an int where it wants a double and
            # answers None, which classify() would then report as a lost connection.
            "sl": float(cmd.sl),
            "tp": float(cmd.tp),
        }
        return self._send_modification(cmd, request, symbol, f"sl {cmd.sl:g} tp {cmd.tp:g}")

    def _to_breakeven(self, cmd: OrderCommand) -> OrderResult:
        """Move the master's own stop to breakeven: past its entry by its round-turn commission
        -- its opening deal's, doubled, else ``commission_per_lot`` -- and ``slippage_points``.

        Worked out here rather than by the engine because this is where the fill's commission,
        the symbol's point and a lot's value can be read. Never loosens: a stop already at or
        past the level is left where it is, as done. And sent only once the closing price is
        past the level, since a stop on the wrong side of the price is refused; until then it
        fails as transient, and the engine asks again later. The result's ``price`` is the
        level."""
        ticket = cmd.slave_ticket
        if ticket is None:
            return self._fail(cmd, error="no ticket to move to breakeven")
        live = self._session.position(ticket)
        if live is UNREADABLE:
            return self._fail(cmd, retryable=True, error=f"could not read position {ticket}")
        if live is None:
            return self._ok(cmd, slave_ticket=ticket, outcome=Outcome.GONE.value, target_gone=True)
        symbol, direction, _ = live
        facts = self._session.symbol_facts(symbol)
        if facts is None:
            return self._fail(
                cmd, slave_symbol=symbol, retryable=True, error=f"no symbol_info for {symbol!r}"
            )

        commission = cmd.commission_per_lot
        try:
            paid = self._session.position_commission(ticket)
        except Exception:
            log.exception("could not read the commission of position %s", ticket)
            paid = None
        if paid is not None:
            charged, lots = paid
            commission = round_turn_per_lot(-charged, lots) or commission
        buffer = cmd.slippage_points * facts.point
        if commission > 0:
            value = self._price_value(direction, symbol, cmd.entry, facts.point)
            if not value:
                return self._fail(
                    cmd,
                    slave_symbol=symbol,
                    retryable=True,
                    error=f"could not price a move on {symbol}, so its commission has no distance",
                )
            buffer += commission / value
        level = stop_past_entry(direction, cmd.entry, buffer, facts.point, facts.digits)

        buy = direction is PositionType.BUY
        if cmd.sl and (cmd.sl >= level if buy else cmd.sl <= level):
            return self._ok(
                cmd,
                slave_ticket=ticket,
                slave_symbol=symbol,
                price=cmd.sl,
                outcome="already past breakeven",
            )
        quote = self._session.tick(symbol)
        if quote is None:
            return self._fail(
                cmd, slave_symbol=symbol, retryable=True, error=f"no tick for {symbol!r}"
            )
        closing = quote[0] if buy else quote[1]
        if not (closing > level if buy else closing < level):
            return self._fail(
                cmd,
                slave_symbol=symbol,
                retryable=True,
                error=f"the price {closing:g} is not past the breakeven stop {level:g} yet",
            )
        request = {
            "action": int(TradeAction.SLTP),
            "symbol": symbol,
            "position": ticket,
            "sl": float(level),
            "tp": float(cmd.tp),
        }
        result = self._send_modification(cmd, request, symbol, f"breakeven sl {level:g}")
        return replace(result, price=level) if result.ok else result

    def _modify_order(self, cmd: OrderCommand) -> OrderResult:
        """Move a copied resting order -- its entry price as well as its stop and target.

        The entry is what makes this a separate operation from :meth:`_modify_sltp`: a
        position's opening price is history and cannot be changed, a resting order's is a
        parameter. Volume is deliberately absent, because MT5 cannot modify a pending order's
        volume at all.

        ``type_time`` is always sent rather than left out, because omitting it would not
        preserve the order's existing expiry -- the zero-filled struct resets it to
        ``ORDER_TIME_GTC``. So the master's expiry is restated on every move, and a master
        clearing its expiry is mirrored by the GTC the zero pair means. The expiry change is
        itself what some of these commands are for: the diff compares it like the levels.
        """
        if cmd.slave_ticket is None:
            return self._fail(cmd, error="no slave ticket recorded for this copy")

        if not self._session.order_exists(cmd.slave_ticket):
            # Filled or removed. Either way the order diff sees it on its next tick and
            # answers with ORDER_FILLED or CANCEL; moving it is no longer meaningful.
            log.info(
                "#%s: slave order %s is no longer resting", cmd.master_ticket, cmd.slave_ticket
            )
            return self._ok(
                cmd,
                slave_ticket=cmd.slave_ticket,
                outcome=Outcome.GONE.value,
                target_gone=True,
                attempts=0,
            )

        # Only so the result line names an instrument -- the request does not need it, and an
        # unresolvable symbol must not stop a modification whose ticket is already known.
        resolved = self._symbols.resolve(
            self._terminal_id,
            cmd.master_symbol,
            self._session.symbol_names(),
            prefix=cmd.symbol_prefix,
            suffix=cmd.symbol_suffix,
            label=self._terminal_id[:8],
            symbol_map=dict(cmd.symbol_map),
        )
        # No expiration_mask here: the order already rests, so no symbol facts were read. A
        # broker that refuses the timed order answers INVALID_EXPIRATION, which
        # :meth:`_send_modification` retries once without the expiry.
        type_time, expiration = self._pending_expiry(cmd, None)
        request = {
            "action": int(TradeAction.MODIFY),
            "order": cmd.slave_ticket,
            "price": float(cmd.entry),
            "sl": float(cmd.sl),
            "tp": float(cmd.tp),
            "type_time": type_time,
            "expiration": expiration,
        }
        return self._send_modification(
            cmd,
            request,
            resolved.name,
            f"entry {cmd.entry:g} sl {cmd.sl:g} tp {cmd.tp:g}",
        )

    def _send_modification(
        self, cmd: OrderCommand, request: dict[str, Any], symbol: str | None, wanted: str
    ) -> OrderResult:
        """Send a modification, with the only retry that makes sense for one.

        Not :meth:`_send`, and not for tidiness: every one of that method's recoveries is a
        fabrication here. There is no market price to re-read on a requote -- these prices are
        the master's, and re-pricing a resting order would drag it onto the market -- and there
        is no filling mode to re-negotiate, because nothing is being filled. Its result
        handling is wrong too: a modification creates no order and no deal, so MT5 returns
        zeroes, which :meth:`_send` would report as a fresh ticket and a zero-lot fill.

        What is left is the busy-server case, and retrying that is safe because a modification
        is idempotent -- a repeat that changes nothing answers ``NO_CHANGES``, which classifies
        as success.

        ``wanted`` describes the levels asked for, so a refusal names them. ``INVALID_STOPS``
        (the stop is inside the broker's minimum distance from the market) is the routine
        rejection here, and "it was refused" without saying what was refused is not enough to
        act on.
        """
        deadline = time.monotonic() + DEADLINE_S
        attempts = 0
        last: Any = None

        while attempts < MAX_ATTEMPTS and time.monotonic() < deadline:
            attempts += 1
            last = self._session.order_send(request)
            retcode = getattr(last, "retcode", None) if last is not None else None
            outcome = classify(retcode)

            if outcome is Outcome.SUCCEEDED:
                return self._ok(
                    cmd,
                    slave_ticket=cmd.slave_ticket,
                    slave_symbol=symbol,
                    retcode=retcode,
                    attempts=attempts,
                    outcome=(
                        "already matched"
                        if retcode == Retcode.NO_CHANGES
                        else Outcome.SUCCEEDED.value
                    ),
                )
            if outcome is Outcome.GONE:
                log.warning(
                    "#%s: %s, so %s was not applied",
                    cmd.master_ticket,
                    _describe(retcode),
                    wanted,
                )
                return self._ok(
                    cmd,
                    slave_ticket=cmd.slave_ticket,
                    slave_symbol=symbol,
                    retcode=retcode,
                    attempts=attempts,
                    outcome=Outcome.GONE.value,
                    target_gone=True,
                )
            if retcode == Retcode.INVALID_EXPIRATION and request.get("expiration"):
                # Only the mirrored expiry was refused -- the copy likely rests GTC from
                # before this broker's refusal was known, or the master's expiry is already
                # in this broker's past. The levels still matter more than the lifetime, so
                # they go again without it; the master's own expiry still cancels the copy.
                log.warning(
                    "#%s: the broker refused the order's expiry; moving it without one",
                    cmd.master_ticket,
                )
                request = {**request, "type_time": int(OrderTime.GTC), "expiration": 0}
                continue
            if outcome is Outcome.BACKOFF:
                time.sleep(BACKOFF_S)
                continue
            break

        retcode = getattr(last, "retcode", None) if last is not None else None
        return self._fail(
            cmd,
            slave_symbol=symbol,
            retcode=retcode,
            attempts=attempts,
            retryable=is_transient(classify(retcode)),
            error=f"{_describe(retcode)} setting {wanted} on {symbol or 'the slave'}",
        )

    # --- sizing ---------------------------------------------------------------------------

    def _size(self, cmd: OrderCommand, symbol: str, facts: SymbolFacts) -> SizingResult:
        if cmd.sizing_basis is SizingBasis.MASTER_LOT_MULTIPLE:
            return lots_for_master_multiple(
                master_volume=cmd.master_volume,
                multiple=cmd.lot_multiple or 1.0,
                facts=facts,
                max_lot=cmd.max_lot,
            )

        entry, sl = widen_for_slippage(
            cmd.entry, cmd.sl, cmd.direction, cmd.slippage_points, facts.point
        )
        loss = self._session.loss_per_lot(cmd.direction, symbol, entry, sl)
        if loss is None:
            # The terminal could not price the move. Sizing anyway would mean inventing the
            # one number the whole calculation rests on.
            log.warning("order_calc_profit gave nothing for %s; cannot size", symbol)
            return SizingResult(failure=SizingFailure.NO_RISK_DISTANCE)
        return lots_for_risk(
            risk_amount=cmd.risk_amount,
            loss_per_lot=loss,
            commission_per_lot=cmd.commission_per_lot,
            facts=facts,
            max_lot=cmd.max_lot,
        )

    # --- sending --------------------------------------------------------------------------

    def _send_with_retries(
        self, cmd: OrderCommand, symbol: str, facts: SymbolFacts, lots: float
    ) -> OrderResult:
        is_pending = cmd.op is ActionKind.PLACE
        price = cmd.entry if is_pending else self._first_tick(symbol, cmd.direction)
        if price is None:
            return self._fail(
                cmd, slave_symbol=symbol, retryable=True, error=f"no tick for {symbol!r}"
            )
        request = self._build_request(
            cmd,
            symbol,
            lots,
            price,
            choose_filling(facts.filling_mask, is_pending=is_pending),
            is_pending,
            expiration_mask=facts.expiration_mask,
        )
        result = self._send(
            cmd,
            request,
            symbol,
            lots,
            facts.filling_mask,
            reprice_direction=None if is_pending else cmd.direction,
        )
        if is_pending and not result.ok and cmd.pending_fallback and price_refused(result.retcode):
            result = self._pending_fallback(cmd, symbol, facts, lots, result)
        if not result.ok:
            return result
        resting = is_pending and not result.filled_at_market
        fill = cmd.entry if resting else (result.price or price)
        return replace(
            result,
            point=facts.point,
            digits=facts.digits,
            price_value=self._price_value(cmd.direction, symbol, fill, facts.point),
        )

    def _pending_fallback(
        self, cmd: OrderCommand, symbol: str, facts: SymbolFacts, lots: float, refused: OrderResult
    ) -> OrderResult:
        """Two more tries for a pending order whose *price* the broker refused.

        The first flips the resting type at the same price. A limit and a stop at one price
        differ only in which side of the quote they may rest on, so when the refusal is the
        quote having already crossed the master's price -- the routine way a copied pending
        dies, since the slave's feed lags and differs from the master's -- the flipped type is
        the one the broker will take, and it still fills at the master's own level.

        The second gives up on resting and takes the market: the master wanted in at this
        price and the slave's broker will not hold an order there, so the nearest honest copy
        is a fill where that slave's market is now. The result says so (``filled_at_market``),
        because the ledger must remember a position, not an order.

        Only for refusals about the price (:func:`~mt5copier.execution.price_refused`):
        anything else -- no money, bad volume, trading disabled -- would fail the same way
        again, so it is returned as the failure it is.
        """
        flipped = cmd.order_type.flipped if cmd.order_type is not None else None
        if flipped is not None:
            log.warning(
                "#%s: %s at %g refused (%s), retrying as a %s",
                cmd.master_ticket,
                cmd.order_type.name if cmd.order_type is not None else "?",
                cmd.entry,
                _describe(refused.retcode),
                flipped.name,
            )
            request = {
                **self._build_request(
                    cmd,
                    symbol,
                    lots,
                    cmd.entry,
                    choose_filling(facts.filling_mask, is_pending=True),
                    True,
                    expiration_mask=facts.expiration_mask,
                ),
                "type": int(flipped),
            }
            result = self._send(
                cmd, request, symbol, lots, facts.filling_mask, reprice_direction=None
            )
            if result.ok:
                return replace(result, placed_type=flipped)
            if not price_refused(result.retcode):
                return result
            refused = result

        log.warning(
            "#%s: no resting order at %g was accepted (%s), executing at market",
            cmd.master_ticket,
            cmd.entry,
            _describe(refused.retcode),
        )
        price = self._first_tick(symbol, cmd.direction)
        if price is None:
            return self._fail(
                cmd,
                slave_symbol=symbol,
                retcode=refused.retcode,
                error=(
                    f"{_describe(refused.retcode)}, and no tick for {symbol!r} to execute "
                    "at market instead"
                ),
            )
        request = self._build_request(
            cmd,
            symbol,
            lots,
            price,
            choose_filling(facts.filling_mask, is_pending=False),
            False,
        )
        result = self._send(
            cmd, request, symbol, lots, facts.filling_mask, reprice_direction=cmd.direction
        )
        return replace(result, filled_at_market=True) if result.ok else result

    def _commission(self, deal: int | None) -> float | None:
        """What the opening deal was charged, as a positive cost; None when there is no deal to
        read, it shows nothing, or the read fails. After the fill, so never allowed to fail it."""
        if not deal:
            return None
        try:
            charged = self._session.deal_commission(deal)
        except Exception:
            log.exception("could not read the commission of deal %s", deal)
            return None
        return -charged if charged is not None and charged < 0 else None

    def _price_value(
        self, direction: PositionType, symbol: str, price: float, point: float
    ) -> float | None:
        """What a 1.0 move in price is worth on one lot, in the account currency, for turning a
        commission into a breakeven distance. Asked of the terminal the way sizing asks it, so
        a cross-currency quote comes out right, and measured over a thousand points rather
        than one so the broker's rounding of the answer does not swamp it.

        After the order has filled, and never allowed to fail it: a None here only means the
        copy cannot go to breakeven while a commission is set.
        """
        if price <= 0 or point <= 0:
            return None
        step = point * 1000
        sl = price - step if direction is PositionType.BUY else price + step
        try:
            loss = self._session.loss_per_lot(direction, symbol, price, sl)
        except Exception:
            log.exception("could not price a move on %s", symbol)
            return None
        return loss / step if loss else None

    def _send(
        self,
        cmd: OrderCommand,
        request: dict[str, Any],
        symbol: str,
        volume: float,
        mask: int,
        *,
        reprice_direction: PositionType | None,
    ) -> OrderResult:
        """Send, classify, and retry within the caps -- shared by every order-shaped request.

        ``reprice_direction`` is the side to re-read the book from on a requote, or None for
        a request whose price is fixed (a pending order rests where it was told to).
        """
        filling = OrderFilling(int(request.get("type_filling", OrderFilling.IOC)))
        price = float(request.get("price", 0.0) or 0.0)
        deadline = time.monotonic() + DEADLINE_S
        attempts = 0
        last: Any = None
        lots = volume

        while attempts < MAX_ATTEMPTS and time.monotonic() < deadline:
            attempts += 1
            last = self._session.order_send(request)
            retcode = getattr(last, "retcode", None) if last is not None else None
            outcome = classify(retcode)

            if outcome in (Outcome.SUCCEEDED, Outcome.PARTIAL):
                filled = float(getattr(last, "volume", lots) or lots)
                if outcome is Outcome.PARTIAL:
                    log.warning(
                        "#%s partially filled: asked %g, got %g", cmd.master_ticket, lots, filled
                    )
                return OrderResult(
                    command_id=cmd.command_id,
                    terminal_id=self._terminal_id,
                    op=cmd.op,
                    master_ticket=cmd.master_ticket,
                    ok=True,
                    retcode=retcode,
                    outcome=outcome.value,
                    slave_ticket=_int_or_none(getattr(last, "order", None)),
                    slave_symbol=symbol,
                    requested_volume=lots,
                    filled_volume=filled,
                    price=_float_or_none(getattr(last, "price", None)) or price,
                    attempts=attempts,
                    # A PLACE is included only when its request became a market deal -- the
                    # fallback's last resort -- whose fill has an opening deal like any
                    # OPEN's. A genuinely resting PLACE has been charged nothing yet.
                    commission=(
                        self._commission(_int_or_none(getattr(last, "deal", None)))
                        if cmd.op is ActionKind.OPEN
                        or (
                            cmd.op is ActionKind.PLACE
                            and int(request.get("action", 0)) == int(TradeAction.DEAL)
                        )
                        else None
                    ),
                )

            if outcome is Outcome.GONE and cmd.op in (
                ActionKind.CLOSE,
                ActionKind.REDUCE,
                ActionKind.BOOK,
            ):
                # The position went between our read and our send. Not open is what was
                # wanted, so this is the desired state reached without us, exactly as an
                # already-missing position is. Guarded by op rather than assumed: this path is
                # shared with OPEN and PLACE, and calling a refused entry a success would
                # record a position that does not exist.
                log.info("#%s: %s", cmd.master_ticket, _describe(retcode))
                return self._ok(
                    cmd,
                    slave_ticket=cmd.slave_ticket,
                    slave_symbol=symbol,
                    retcode=retcode,
                    outcome=Outcome.GONE.value,
                    target_gone=True,
                    filled_volume=0.0,
                    attempts=attempts,
                )

            if outcome is Outcome.REPRICE and reprice_direction is not None:
                fresh = self._market_price(symbol, reprice_direction)
                if fresh is None:
                    break
                price = fresh
                request = {**request, "price": price}
                continue
            if outcome is Outcome.REFILL:
                nxt = alternative_filling(filling, mask)
                if nxt is None:
                    break
                log.info(
                    "#%s: %s refused, retrying as %s", cmd.master_ticket, filling.name, nxt.name
                )
                filling = nxt
                request = {**request, "type_filling": int(nxt)}
                continue
            if retcode == Retcode.INVALID_EXPIRATION and request.get("expiration"):
                # The mirrored expiry, not the order, is what was refused -- a broker rule
                # the expiration mask did not disclose, or an expiry already in this broker's
                # past. Without it the request is exactly what every copy was before expiry
                # was mirrored at all, and losing the trade over the lifetime would be
                # backwards; the master's own expiry still cancels the copy.
                log.warning(
                    "#%s: the broker refused the order's expiry; placing it GTC instead",
                    cmd.master_ticket,
                )
                request = {**request, "type_time": int(OrderTime.GTC), "expiration": 0}
                continue
            if outcome is Outcome.BACKOFF:
                time.sleep(BACKOFF_S)
                continue
            break

        retcode = getattr(last, "retcode", None) if last is not None else None
        return self._fail(
            cmd,
            slave_symbol=symbol,
            retcode=retcode,
            requested_volume=lots,
            attempts=attempts,
            # Describes the failure, not what to do about it: the ledger decides whether a
            # transient one is worth deriving again, and never is for an OPEN or a PLACE.
            retryable=is_transient(classify(retcode)),
            error=f"{_describe(retcode)} after {attempts} attempt(s)",
        )

    def _build_request(
        self,
        cmd: OrderCommand,
        symbol: str,
        lots: float,
        price: float,
        filling: OrderFilling,
        is_pending: bool,
        expiration_mask: int = 0,
    ) -> dict[str, Any]:
        """The MqlTradeRequest, as a plain dict -- what the Python API expects."""
        order_type = (
            int(cmd.order_type)
            if (is_pending and cmd.order_type is not None)
            else (0 if cmd.direction is PositionType.BUY else 1)
        )
        type_time, expiration = (
            self._pending_expiry(cmd, expiration_mask) if is_pending else (int(OrderTime.GTC), 0)
        )
        request: dict[str, Any] = {
            # TRADE_ACTION_PENDING is 5; TRADE_ACTION_DEAL is 1.
            "action": 5 if is_pending else 1,
            "symbol": symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "deviation": cmd.deviation_points,
            "magic": cmd.magic,
            "comment": cmd.comment[:31],
            "type_filling": int(filling),
            "type_time": type_time,
        }
        if expiration:
            request["expiration"] = expiration
        if cmd.sl:
            request["sl"] = cmd.sl
        if cmd.tp:
            request["tp"] = cmd.tp
        return request

    def _pending_expiry(self, cmd: OrderCommand, expiration_mask: int | None) -> tuple[int, int]:
        """The ``(type_time, expiration)`` a pending request carries, mirroring the master's.

        The command's expiry is real UTC; MT5 takes expirations in the *slave broker's* own
        clock, so the slave's offset puts it back on that clock -- each side converts through
        UTC precisely because the two brokers' clocks need not agree.

        Three cases fall back to GTC, each still covered by the mirror: when the master's
        order expires, it leaves the master's book and the diff cancels the copy. No expiry
        to mirror is the ordinary one. A symbol whose reported ``expiration_mode`` does not
        permit a timed order cannot carry one (a mask of zero is "the terminal said nothing"
        and is allowed through, like a zero filling mask -- a wrong guess is recoverable, see
        :meth:`_send`). And while the slave's clock offset is unknown the timestamp could
        land hours off, so resting GTC for the moment is the honest copy.
        """
        if not cmd.expiration_utc:
            return int(OrderTime.GTC), 0
        if expiration_mask and not expiration_mask & SymbolExpiration.SPECIFIED:
            log.warning(
                "#%s: this broker does not allow a timed pending on the symbol "
                "(expiration mask %s); resting GTC, the master's expiry still cancels it",
                cmd.master_ticket,
                expiration_mask,
            )
            return int(OrderTime.GTC), 0
        offset = self._session.server_offset_s()
        if offset is None:
            log.warning(
                "#%s: the broker's clock offset is not known yet, so the master's expiry "
                "cannot be placed on it; resting GTC for now",
                cmd.master_ticket,
            )
            return int(OrderTime.GTC), 0
        return int(OrderTime.SPECIFIED), cmd.expiration_utc + offset

    def _market_price(self, symbol: str, direction: PositionType) -> float | None:
        """Buy at the ask, sell at the bid. Re-read on every reprice, never cached."""
        quote = self._session.tick(symbol)
        if quote is None:
            return None
        bid, ask = quote
        price = ask if direction is PositionType.BUY else bid
        return price if price > 0 else None

    def _first_tick(self, symbol: str, direction: PositionType) -> float | None:
        """:meth:`_market_price`, tolerant of a symbol whose quote has not started yet.

        A symbol can be selected and visible in the terminal's own Market Watch -- confirmed
        live in its UI -- while ``symbol_info_tick`` still answers nothing for a stretch
        afterward. A failed OPEN is final (``reconcile.py``'s ``apply_result``), so one unlucky
        read here is not a retry later, it is the trade gone; one retry after a short wait is
        cheap insurance against exactly the gap :meth:`symbol_facts` already treats the same
        way for a select that cannot be acted on instantly.

        Only called for the first price read of a fresh OPEN. Repricing mid-retry in
        :meth:`_send` stays on the bare :meth:`_market_price` -- that path already has its own
        attempt budget (``MAX_ATTEMPTS``, ``DEADLINE_S``), and a symbol already mid-flight has
        necessarily ticked at least once already.
        """
        price = self._market_price(symbol, direction)
        if price is not None:
            return price
        time.sleep(2.0)
        return self._market_price(symbol, direction)

    # --- helpers --------------------------------------------------------------------------

    def _fail(self, cmd: OrderCommand, **kw: Any) -> OrderResult:
        kw.setdefault("outcome", Outcome.FATAL.value)
        return OrderResult(
            command_id=cmd.command_id,
            terminal_id=self._terminal_id,
            op=cmd.op,
            master_ticket=cmd.master_ticket,
            ok=False,
            **kw,
        )

    def _ok(self, cmd: OrderCommand, **kw: Any) -> OrderResult:
        kw.setdefault("outcome", Outcome.SUCCEEDED.value)
        return OrderResult(
            command_id=cmd.command_id,
            terminal_id=self._terminal_id,
            op=cmd.op,
            master_ticket=cmd.master_ticket,
            ok=True,
            **kw,
        )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None
