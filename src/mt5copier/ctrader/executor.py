"""Turning an :class:`~mt5copier.protocol.OrderCommand` into a cTrader order: the twin of
:mod:`mt5copier.executor`, which it follows rule for rule wherever the two brokers allow.

The same order of operations -- refuse a live account, resolve the symbol, size, send -- and the
same rule that a position already gone is a close or a modification **done**, not failed, as
long as "gone" and "could not read it" stay apart (a read that fails raises, and is reported as
a transient failure).

Where cTrader differs:

* **One send, no retry loop.** There are no requotes or filling modes to renegotiate; the server
  fills a market order at the market or refuses it with a reason.
* **An answer, then the fill.** A request is answered when it is *accepted*; the fill arrives
  after it as an execution event. Only the fill makes a copy, so this waits for it, setting
  aside every other event for the worker to read.
* **A market order takes no absolute stop.** It is sent with the master's entry-to-stop and
  entry-to-target distances as *relative* levels, so the copy is never unprotected, and moved
  to the exact levels once it has filled.
* **No ``order_calc_profit``.** Risk sizing works out a lot's value itself (see
  :func:`~mt5copier.ctrader.convert.price_value`), which it can only do for a symbol quoted
  in the account's currency or based on it. Any other symbol is refused until prices are read.
* **Resting orders are not built yet**: placing, moving and cancelling them are refused.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..execution import Outcome
from ..logging_setup import get_logger
from ..protocol import ActionKind, OrderCommand, OrderResult, PositionType, SizingBasis
from ..sizing import (
    SizingResult,
    SymbolFacts,
    floor_to_step,
    lots_for_master_multiple,
    lots_for_risk,
    quantize_volume,
    widen_for_slippage,
)
from ..symbols import SymbolCache
from . import convert as cv
from .client import DISCONNECT_EVENTS, OpenApiError
from .proto import OpenApiMessages_pb2 as oa
from .proto import OpenApiModelMessages_pb2 as om

if TYPE_CHECKING:
    from .worker import _Account

log = get_logger(__name__)

FILL_WAIT_S = 10.0
"""How long an accepted order may take to report its fill. Past this the copy is reported as
failed with a warning to look at the account: it may still fill, and would then show there as
one of ours that nothing tracks."""

PENDINGS_NOT_YET = "resting orders on cTrader are not built yet"

_TRANSIENT = frozenset(
    {
        "REQUEST_FREQUENCY_EXCEEDED",
        "BLOCKED_PAYLOAD_TYPE",
        "SERVER_IS_UNDER_MAINTENANCE",
        "CH_SERVER_NOT_REACHABLE",
        "PENDING_EXECUTION",
        "POSITION_LOCKED",
    }
)
"""Refusals about the moment rather than the request."""

_GONE = frozenset({"POSITION_NOT_FOUND", "POSITION_NOT_OPEN"})

_FILLED = (om.ORDER_FILLED,)
_ENDED = (om.ORDER_REJECTED, om.ORDER_CANCELLED, om.ORDER_EXPIRED)


@dataclass(slots=True)
class _Answer:
    """How a request ended: an execution event, a refusal's code, or no ending in time."""

    event: Any = None
    code: str | None = None
    description: str = ""
    timed_out: bool = False

    @property
    def refusal(self) -> str:
        return f"{self.code}: {self.description}" if self.description else str(self.code)


class CTraderExecutor:
    """Executes commands against one cTrader account. One per worker process."""

    def __init__(self, terminal_id: str, account: _Account) -> None:
        self._terminal_id = terminal_id
        self._account = account
        self._symbols = SymbolCache()

    def forget_symbols(self) -> None:
        self._symbols.forget(self._terminal_id)

    # --- the entry point ------------------------------------------------------------------

    async def execute(self, cmd: OrderCommand) -> OrderResult:
        """Do one command. Never raises: a crash here must not take the worker down."""
        try:
            return await self._execute(cmd)
        except OpenApiError as exc:
            return self._fail(
                cmd, retryable=exc.code in _TRANSIENT, error=f"cTrader refused: {exc}"
            )
        except (OSError, TimeoutError, ConnectionError) as exc:
            sent = (
                " (an order may have gone out: check the account)"
                if cmd.op is ActionKind.OPEN
                else ""
            )
            return self._fail(cmd, retryable=True, error=f"lost cTrader ({exc!r}){sent}")
        except Exception as exc:
            log.exception("executing %s for #%s crashed", cmd.op.value, cmd.master_ticket)
            return self._fail(cmd, error=f"executor crashed: {exc!r}")

    async def _execute(self, cmd: OrderCommand) -> OrderResult:
        if self._account.is_live and not cmd.allow_live:
            log.error(
                "refusing to trade a live cTrader account; set allow_live_trading on this "
                "terminal if that is genuinely intended"
            )
            return self._fail(cmd, error="account is not a demo (a live cTrader account); refused")
        if not self._account.connected:
            return self._fail(cmd, retryable=True, error="not connected to cTrader")
        if cmd.op in (ActionKind.CLOSE, ActionKind.REDUCE, ActionKind.BOOK):
            return await self._close(cmd)
        if cmd.op is ActionKind.MODIFY_SLTP:
            return await self._modify_sltp(cmd)
        if cmd.op is ActionKind.OPEN:
            return await self._open(cmd)
        if cmd.op in (ActionKind.PLACE, ActionKind.MODIFY_ORDER, ActionKind.CANCEL):
            return self._fail(cmd, error=PENDINGS_NOT_YET)
        return self._fail(cmd, error=f"nothing to send for {cmd.op.value}")

    # --- opening ----------------------------------------------------------------------------

    async def _open(self, cmd: OrderCommand) -> OrderResult:
        account = self._account
        resolved = self._symbols.resolve(
            self._terminal_id,
            cmd.master_symbol,
            account.symbol_ids.keys(),
            prefix=cmd.symbol_prefix,
            suffix=cmd.symbol_suffix,
            label=self._terminal_id[:16],
            symbol_map=dict(cmd.symbol_map),
        )
        if not resolved.ok or resolved.name is None:
            return self._fail(cmd, error=f"symbol {cmd.master_symbol!r} not found on this slave")
        name = resolved.name
        sid = account.symbol_ids[name]
        await account.symbols_for({sid})
        symbol = account.symbols.get(sid)
        if symbol is None:
            return self._fail(cmd, slave_symbol=name, error=f"cTrader gave no details for {name}")
        facts = cv.facts(symbol)

        sizing = self._size(cmd, symbol, facts)
        if isinstance(sizing, str):
            return self._fail(cmd, slave_symbol=name, error=sizing)
        if not sizing.ok or sizing.lots is None:
            return self._fail(cmd, slave_symbol=name, error=sizing.describe())
        log.info("#%s on %s: %s", cmd.master_ticket, name, sizing.describe())
        lots = sizing.lots
        if cmd.max_margin is not None:
            fitted = await self._fit_margin(cmd, symbol, facts, lots, cmd.max_margin)
            if isinstance(fitted, str):
                return self._fail(cmd, slave_symbol=name, error=fitted)
            lots = fitted

        request = oa.ProtoOANewOrderReq(
            ctidTraderAccountId=account.account_id,
            symbolId=sid,
            orderType=om.MARKET,
            tradeSide=cv.trade_side(cmd.direction),
            volume=symbol.volume(lots),
            label=cv.COPIER_LABEL,
            comment=cmd.comment[:512],
        )
        stop, target = _relative_levels(cmd, symbol.digits)
        if stop:
            request.relativeStopLoss = stop
        if target:
            request.relativeTakeProfit = target

        answer = await self._follow(request, wait_for_fill=True)
        event = answer.event
        opened = _opened(event)
        if answer.code is not None:
            return self._fail(
                cmd,
                slave_symbol=name,
                requested_volume=lots,
                attempts=1,
                retryable=answer.code in _TRANSIENT,
                error=f"cTrader refused the order: {answer.refusal}",
            )
        if not opened:
            if answer.timed_out:
                error = (
                    f"cTrader accepted the order but reported no fill within {FILL_WAIT_S:g}s: "
                    "check the account"
                )
            else:
                error = f"the order ended unfilled ({_execution_name(event)}{_why(event)})"
            return self._fail(
                cmd, slave_symbol=name, requested_volume=lots, attempts=1, error=error
            )

        position = event.position
        ticket = int(position.positionId)
        filled = symbol.lots(int(position.tradeData.volume))
        price = float(event.deal.executionPrice) if event.HasField("deal") else 0.0
        price = price or float(position.price)
        partial = int(event.executionType) not in _FILLED
        if partial:
            log.warning("#%s partially filled: asked %g, got %g", cmd.master_ticket, lots, filled)
        outcome = Outcome.PARTIAL.value if partial else Outcome.SUCCEEDED.value

        if cmd.sl or cmd.tp:
            # The copy exists now, so nothing here may turn it into a failure: the ledger would
            # forget a position the account holds.
            try:
                exact = await self._amend(ticket, cmd.sl, cmd.tp, symbol.digits)
            except (OpenApiError, OSError, TimeoutError, ConnectionError) as exc:
                exact = repr(exc)
            if exact is not None:
                log.error(
                    "#%s: copy %s opened but its stops were not set exactly (%s); it carries "
                    "the master's distances from its own fill instead",
                    cmd.master_ticket,
                    ticket,
                    exact,
                )
                outcome = f"{outcome}; stops only approximate: {exact}"

        return OrderResult(
            command_id=cmd.command_id,
            terminal_id=self._terminal_id,
            op=cmd.op,
            master_ticket=cmd.master_ticket,
            ok=True,
            outcome=outcome,
            slave_ticket=ticket,
            slave_symbol=name,
            requested_volume=lots,
            filled_volume=filled,
            price=price or None,
            attempts=1,
            point=symbol.point,
            digits=symbol.digits,
            price_value=cv.price_value(symbol, account.deposit_asset_id, price),
        )

    def _size(
        self, cmd: OrderCommand, symbol: cv.SymbolInfo, facts: SymbolFacts
    ) -> SizingResult | str:
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
        loss = cv.loss_per_lot(symbol, self._account.deposit_asset_id, entry, sl)
        if loss is None:
            currency = self._account.currency or "the account's currency"
            return (
                f"cannot risk-size {symbol.name} on cTrader yet: it is quoted in neither "
                f"{currency} nor its own base, and converting needs live prices"
            )
        return lots_for_risk(
            risk_amount=cmd.risk_amount,
            loss_per_lot=loss,
            commission_per_lot=cmd.commission_per_lot,
            facts=facts,
            max_lot=cmd.max_lot,
        )

    async def _fit_margin(
        self, cmd: OrderCommand, symbol: cv.SymbolInfo, facts: SymbolFacts, lots: float, cap: float
    ) -> float | str:
        """:meth:`mt5copier.executor.Executor._fit_margin`, with the margin asked of cTrader."""
        margin = await self._margin(cmd.direction, symbol, lots)
        warn_only = cmd.margin_cap == "warn"
        name = symbol.name
        if margin is None:
            if warn_only:
                log.warning("#%s: could not price its margin on %s", cmd.master_ticket, name)
                return lots
            return f"could not price the margin of {lots:g} lots on {name}, which a guard limits"
        if margin <= cap:
            return lots
        if warn_only:
            log.warning(
                "#%s: %g lots on %s use %.2f margin, over the %.2f a guard allows",
                cmd.master_ticket,
                lots,
                name,
                margin,
                cap,
            )
            return lots
        if cmd.margin_cap == "shrink":
            fitted = floor_to_step(lots * cap / margin, facts.volume_step)
            if fitted >= facts.volume_min:
                log.warning(
                    "#%s: shrunk from %g to %g lots on %s to fit a guard's %.2f margin",
                    cmd.master_ticket,
                    lots,
                    fitted,
                    name,
                    cap,
                )
                return fitted
            return (
                f"even {facts.volume_min:g} lots on {name} needs more than the "
                f"{cap:.2f} margin a guard allows"
            )
        return (
            f"{lots:g} lots on {name} need {margin:.2f} margin, over the {cap:.2f} a guard allows"
        )

    async def _margin(
        self, direction: PositionType, symbol: cv.SymbolInfo, lots: float
    ) -> float | None:
        account = self._account
        assert account.client is not None
        answer = await account.client.request(
            oa.ProtoOAExpectedMarginReq(
                ctidTraderAccountId=account.account_id,
                symbolId=symbol.symbol_id,
                volume=[symbol.volume(lots)],
            )
        )
        if not answer.margin:
            return None
        quote = answer.margin[0]
        raw = quote.buyMargin if direction is PositionType.BUY else quote.sellMargin
        return cv.money(int(raw), int(answer.moneyDigits))

    # --- closing ----------------------------------------------------------------------------

    async def _close(self, cmd: OrderCommand) -> OrderResult:
        """Close a copy in full or in part, at the volume cTrader holds now rather than the one
        on record -- see :meth:`mt5copier.executor.Executor._close_or_reduce`."""
        if cmd.slave_ticket is None:
            return self._fail(cmd, error="no slave ticket recorded for this copy")
        account = self._account
        raw = (await account.open_positions()).get(cmd.slave_ticket)
        if raw is None:
            log.info("#%s: slave position %s already gone", cmd.master_ticket, cmd.slave_ticket)
            return self._ok(
                cmd, slave_ticket=cmd.slave_ticket, filled_volume=0.0, outcome="already closed"
            )
        sid = int(raw.tradeData.symbolId)
        await account.symbols_for({sid})
        symbol = account.symbols[sid]
        open_cents = int(raw.tradeData.volume)
        volume = open_cents

        if cmd.op in (ActionKind.REDUCE, ActionKind.BOOK):
            fraction = cmd.close_fraction or 0.0
            if not 0.0 < fraction < 1.0:
                return self._fail(cmd, error=f"nonsensical close fraction {fraction!r}")
            facts = cv.facts(symbol)
            open_lots = symbol.lots(open_cents)
            sized = quantize_volume(open_lots * fraction, facts)
            if cmd.op is ActionKind.BOOK:
                left = open_lots - (sized.lots or 0.0)
                if not sized.ok or sized.lots is None or left < facts.volume_min - 1e-9:
                    words = (
                        f"{fraction:.0%} of {open_lots:g} lots"
                        + (f" would leave {left:g}" if sized.ok else f": {sized.describe()}")
                        + f"; the minimum on {symbol.name} is {facts.volume_min:g}"
                    )
                    return self._fail(
                        cmd,
                        slave_symbol=symbol.name,
                        unsplittable=True,
                        error=f"cannot book {words}",
                    )
            if not sized.ok or sized.lots is None:
                return self._fail(
                    cmd, slave_symbol=symbol.name, error=f"partial close {sized.describe()}"
                )
            volume = min(symbol.volume(sized.lots), open_cents)

        answer = await self._follow(
            oa.ProtoOAClosePositionReq(
                ctidTraderAccountId=account.account_id,
                positionId=cmd.slave_ticket,
                volume=volume,
            ),
            wait_for_fill=True,
        )
        if answer.code in _GONE:
            log.info("#%s: %s", cmd.master_ticket, answer.refusal)
            return self._ok(
                cmd,
                slave_ticket=cmd.slave_ticket,
                slave_symbol=symbol.name,
                outcome=Outcome.GONE.value,
                target_gone=True,
                filled_volume=0.0,
                attempts=1,
            )
        event = answer.event
        lots = symbol.lots(volume)
        if (
            answer.code is not None
            or event is None
            or int(event.executionType)
            not in (
                *_FILLED,
                om.ORDER_PARTIAL_FILL,
            )
        ):
            if answer.code is not None:
                error = f"cTrader refused the close: {answer.refusal}"
            elif answer.timed_out:
                error = f"cTrader reported no fill for the close within {FILL_WAIT_S:g}s"
            else:
                error = f"the close ended unfilled ({_execution_name(event)}{_why(event)})"
            return self._fail(
                cmd,
                slave_symbol=symbol.name,
                requested_volume=lots,
                attempts=1,
                retryable=answer.timed_out or answer.code in _TRANSIENT,
                error=error,
            )
        deal = event.deal if event.HasField("deal") else None
        filled = symbol.lots(int(deal.filledVolume)) if deal is not None else lots
        return self._ok(
            cmd,
            slave_ticket=cmd.slave_ticket,
            slave_symbol=symbol.name,
            requested_volume=lots,
            filled_volume=filled or lots,
            price=(float(deal.executionPrice) or None) if deal is not None else None,
            attempts=1,
        )

    # --- modifying ----------------------------------------------------------------------------

    async def _modify_sltp(self, cmd: OrderCommand) -> OrderResult:
        """Move a copy's stop and target to the master's -- both at once, a zero clearing one,
        as on MT5."""
        if cmd.slave_ticket is None:
            return self._fail(cmd, error="no slave ticket recorded for this copy")
        account = self._account
        raw = (await account.open_positions()).get(cmd.slave_ticket)
        wanted = f"sl {cmd.sl:g} tp {cmd.tp:g}"
        if raw is None:
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
            )
        sid = int(raw.tradeData.symbolId)
        await account.symbols_for({sid})
        symbol = account.symbols[sid]
        digits = symbol.digits
        if round(float(raw.stopLoss), digits) == round(cmd.sl, digits) and round(
            float(raw.takeProfit), digits
        ) == round(cmd.tp, digits):
            return self._ok(
                cmd,
                slave_ticket=cmd.slave_ticket,
                slave_symbol=symbol.name,
                outcome="already matched",
            )
        refusal = await self._amend(cmd.slave_ticket, cmd.sl, cmd.tp, digits)
        if refusal is None:
            return self._ok(
                cmd, slave_ticket=cmd.slave_ticket, slave_symbol=symbol.name, attempts=1
            )
        if refusal.split(":", 1)[0] in _GONE:
            log.warning("#%s: %s, so %s was not applied", cmd.master_ticket, refusal, wanted)
            return self._ok(
                cmd,
                slave_ticket=cmd.slave_ticket,
                slave_symbol=symbol.name,
                outcome=Outcome.GONE.value,
                target_gone=True,
                attempts=1,
            )
        return self._fail(
            cmd,
            slave_symbol=symbol.name,
            attempts=1,
            retryable=refusal.split(":", 1)[0] in _TRANSIENT,
            error=f"{refusal} setting {wanted} on {symbol.name}",
        )

    async def _amend(self, ticket: int, sl: float, tp: float, digits: int) -> str | None:
        """Set a position's stop and target exactly. None when done, else cTrader's refusal.

        A level left out of the request is removed, so a zero is sent by leaving it out. Tried
        twice when the position is still busy with the order that opened it."""
        request = oa.ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=self._account.account_id, positionId=ticket
        )
        if sl:
            request.stopLoss = round(sl, digits)
        if tp:
            request.takeProfit = round(tp, digits)
        answer = await self._follow(request, wait_for_fill=False)
        if answer.code in ("PENDING_EXECUTION", "POSITION_LOCKED"):
            await asyncio.sleep(0.5)
            answer = await self._follow(request, wait_for_fill=False)
        if answer.code is not None:
            return answer.refusal
        if answer.event is not None and int(answer.event.executionType) in _ENDED:
            return f"{_execution_name(answer.event)}{_why(answer.event)}"
        return None

    # --- sending ------------------------------------------------------------------------------

    async def _follow(self, request: Any, *, wait_for_fill: bool) -> _Answer:
        """Send ``request`` and follow its order to an ending.

        The answer to a request is its first execution event, usually ``ORDER_ACCEPTED``. With
        ``wait_for_fill``, the events after it are read until the same order fills, partly
        fills and stops, or ends unfilled. Every other event is put back, in order, for the
        worker. An error answer (``ProtoOAErrorRes``) raises; a refusal of the order
        (``ProtoOAOrderErrorEvent``) is returned."""
        account = self._account
        client = account.client
        assert client is not None
        first = await client.request(request)
        if isinstance(first, oa.ProtoOAOrderErrorEvent):
            return _Answer(code=str(first.errorCode), description=str(first.description))
        if not isinstance(first, oa.ProtoOAExecutionEvent):
            return _Answer(code="UNEXPECTED", description=type(first).__name__)
        account.refresh_soon()
        if not wait_for_fill or _ended(first):
            return _Answer(event=first)

        order_id = int(first.order.orderId)
        last = first
        kept: list[Any] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FILL_WAIT_S
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return _Answer(event=last, timed_out=True)
                try:
                    event = await asyncio.wait_for(client.events.get(), remaining)
                except TimeoutError:
                    return _Answer(event=last, timed_out=True)
                if isinstance(event, oa.ProtoOAOrderErrorEvent) and int(event.orderId) == order_id:
                    return _Answer(code=str(event.errorCode), description=str(event.description))
                if isinstance(event, oa.ProtoOAExecutionEvent) and (
                    event.HasField("order") and int(event.order.orderId) == order_id
                ):
                    last = event
                    if _ended(event):
                        return _Answer(event=event)
                    continue
                kept.append(event)
                if isinstance(event, DISCONNECT_EVENTS):
                    return _Answer(event=last, timed_out=True)
        finally:
            for event in kept:
                client.events.put_nowait(event)

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


def _opened(event: Any) -> bool:
    """Whether an execution event shows the order's position open with volume in it -- after
    a fill, a partial fill, or a partial fill whose remainder was then cancelled."""
    if event is None or int(event.executionType) == om.ORDER_ACCEPTED:
        return False
    if not event.HasField("position"):
        return False
    position = event.position
    return (
        int(position.positionStatus) == om.POSITION_STATUS_OPEN
        and int(position.tradeData.volume) > 0
    )


def _ended(event: Any) -> bool:
    return int(event.executionType) in (*_FILLED, *_ENDED)


def _execution_name(event: Any) -> str:
    return om.ProtoOAExecutionType.Name(int(event.executionType)) if event is not None else "?"


def _why(event: Any) -> str:
    return f": {event.errorCode}" if event is not None and event.errorCode else ""


def _relative_levels(cmd: OrderCommand, digits: int) -> tuple[int, int]:
    """The master's stop and target as distances from its entry, for a market order -- each
    only when it lies on its proper side of the entry. A stop already moved past the entry
    cannot be written as a distance in the losing direction; that one waits for the amend."""
    buy = cmd.direction is PositionType.BUY
    stop = target = 0
    if cmd.entry > 0 and cmd.sl > 0 and (cmd.sl < cmd.entry if buy else cmd.sl > cmd.entry):
        stop = cv.relative(cmd.entry - cmd.sl, digits)
    if cmd.entry > 0 and cmd.tp > 0 and (cmd.tp > cmd.entry if buy else cmd.tp < cmd.entry):
        target = cv.relative(cmd.tp - cmd.entry, digits)
    return stop, target
