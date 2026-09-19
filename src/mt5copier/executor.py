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
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from .execution import (
    OrderFilling,
    Outcome,
    TradeAction,
    alternative_filling,
    choose_filling,
    classify,
)
from .execution import describe_retcode as _describe
from .logging_setup import get_logger
from .protocol import ActionKind, OrderCommand, OrderResult, PositionType, SizingBasis
from .sizing import (
    SizingFailure,
    SizingResult,
    SymbolFacts,
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

ACCOUNT_TRADE_MODE_DEMO = 0
"""``ACCOUNT_TRADE_MODE_DEMO``. Contest is 1 and real is 2."""


class Session(Protocol):
    """What the executor needs from a terminal. Implemented by the worker's ``_Session``."""

    def account_trade_mode(self) -> int | None: ...
    def symbol_names(self) -> list[str]: ...
    def symbol_facts(self, name: str) -> SymbolFacts | None: ...
    def tick(self, name: str) -> tuple[float, float] | None: ...
    def position(self, ticket: int) -> tuple[str, PositionType, float] | None: ...
    def order_exists(self, ticket: int) -> bool: ...
    def loss_per_lot(
        self, direction: PositionType, symbol: str, entry: float, sl: float
    ) -> float | None: ...
    def order_send(self, request: dict[str, Any]) -> Any: ...


class Executor:
    """Executes commands against one terminal. One per worker process."""

    def __init__(self, terminal_id: str, session: Session) -> None:
        self._terminal_id = terminal_id
        self._session = session
        self._symbols = SymbolCache()
        """Resolution is cached per (terminal, master symbol); the scan is the expensive part
        and a broker's symbol list does not change under a running terminal."""

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

        if cmd.op in (ActionKind.CLOSE, ActionKind.REDUCE):
            return self._close_or_reduce(cmd)
        if cmd.op is ActionKind.CANCEL:
            return self._cancel(cmd)
        if cmd.op in (ActionKind.MODIFY_SLTP, ActionKind.MODIFY_ORDER):
            # Kept as a backstop, not a normal path: these are absent from
            # protocol.DISPATCHABLE_KINDS, so the engine does not send them at all. Add them
            # there when this branch is implemented, or SL/TP sync stays silently off.
            return self._fail(cmd, error="modification arrives with stage 3")
        if cmd.op not in (ActionKind.OPEN, ActionKind.PLACE):
            return self._fail(cmd, error=f"nothing to send for {cmd.op.value}")

        resolved = self._symbols.resolve(
            self._terminal_id,
            cmd.master_symbol,
            self._session.symbol_names(),
            prefix=cmd.symbol_prefix,
            suffix=cmd.symbol_suffix,
            label=self._terminal_id[:8],
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

        return self._send_with_retries(cmd, symbol, facts, sizing.lots)

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
        if live is None:
            log.info("#%s: slave position %s already gone", cmd.master_ticket, cmd.slave_ticket)
            return self._ok(
                cmd,
                slave_ticket=cmd.slave_ticket,
                filled_volume=0.0,
                outcome="already closed",
            )
        symbol, direction, open_volume = live

        volume = open_volume
        if cmd.op is ActionKind.REDUCE:
            fraction = cmd.close_fraction or 0.0
            if not 0.0 < fraction < 1.0:
                return self._fail(cmd, error=f"nonsensical close fraction {fraction!r}")
            facts = self._session.symbol_facts(symbol)
            if facts is None:
                return self._fail(cmd, slave_symbol=symbol, error=f"no symbol_info for {symbol!r}")
            sized = quantize_volume(open_volume * fraction, facts)
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
            return self._fail(cmd, slave_symbol=symbol, error=f"no tick for {symbol!r}")

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
        if outcome is Outcome.SUCCEEDED:
            return self._ok(cmd, slave_ticket=cmd.slave_ticket, outcome=outcome.value, attempts=1)
        return self._fail(
            cmd,
            retcode=retcode,
            attempts=1,
            error=f"{_describe(retcode)} cancelling order {cmd.slave_ticket}",
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
        price = cmd.entry if is_pending else self._market_price(symbol, cmd.direction)
        if price is None:
            return self._fail(cmd, slave_symbol=symbol, error=f"no tick for {symbol!r}")
        request = self._build_request(
            cmd,
            symbol,
            lots,
            price,
            choose_filling(facts.filling_mask, is_pending=is_pending),
            is_pending,
        )
        return self._send(
            cmd,
            request,
            symbol,
            lots,
            facts.filling_mask,
            reprice_direction=None if is_pending else cmd.direction,
        )

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
    ) -> dict[str, Any]:
        """The MqlTradeRequest, as a plain dict -- what the Python API expects."""
        order_type = (
            int(cmd.order_type)
            if (is_pending and cmd.order_type is not None)
            else (0 if cmd.direction is PositionType.BUY else 1)
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
            # ORDER_TIME_GTC
            "type_time": 0,
        }
        if cmd.sl:
            request["sl"] = cmd.sl
        if cmd.tp:
            request["tp"] = cmd.tp
        return request

    def _market_price(self, symbol: str, direction: PositionType) -> float | None:
        """Buy at the ask, sell at the bid. Re-read on every reprice, never cached."""
        quote = self._session.tick(symbol)
        if quote is None:
            return None
        bid, ask = quote
        price = ask if direction is PositionType.BUY else bid
        return price if price > 0 else None

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
