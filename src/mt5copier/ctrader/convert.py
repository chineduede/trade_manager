"""cTrader's messages, turned into the copier's own types.

The rest of the copier was written against MT5, so a cTrader account is made to look like one:
lots rather than cTrader's hundredths of a unit, MT5's order-type numbers, seconds rather than
milliseconds. Pure functions, so they are tested without a connection.

Where they differ in kind rather than in units:

* **Volume.** cTrader counts it in hundredths of a unit of the base asset, and so is a symbol's
  ``lotSize``; lots are the one divided by the other.
* **Ours.** MT5 marks an order with a number (the magic); cTrader with a string, the ``label``.
  A position carrying :data:`COPIER_LABEL` is reported with :data:`COPIER_MAGIC`, so the
  reconciler tells the copier's own trades apart exactly as it does on MT5.
* **Time.** cTrader stamps everything in real UTC milliseconds, so the broker clock offset MT5
  needs is always zero here.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..protocol import COPIER_MAGIC, OrderType, PendingOrder, Position, PositionLive, PositionType

COPIER_LABEL = "mt5copier"
"""Put on every order the copier sends to cTrader, as :data:`COPIER_MAGIC` is on MT5.
**Fixed once live copies carry it**, for the same reason the magic is."""

# ProtoOATradeSide
_BUY = 1
# ProtoOAOrderType
_LIMIT = 2
_STOP = 3
_STOP_LIMIT = 6


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    """What converting a symbol's volumes and prices needs."""

    symbol_id: int
    name: str
    digits: int
    lot_size: int
    """In hundredths of a unit, like every volume."""

    def lots(self, volume: int) -> float:
        return volume / self.lot_size if self.lot_size else 0.0


def money(value: int, digits: int) -> float:
    """cTrader's money fields are integers scaled by the account's ``moneyDigits``."""
    return float(value) / float(10**digits)


def _direction(trade_side: int) -> PositionType:
    return PositionType.BUY if trade_side == _BUY else PositionType.SELL


def position(raw: object, symbol: SymbolInfo) -> Position:
    """One ``ProtoOAPosition``."""
    data = raw.tradeData  # type: ignore[attr-defined]
    return Position(
        ticket=int(raw.positionId),  # type: ignore[attr-defined]
        symbol=symbol.name,
        type=_direction(int(data.tradeSide)),
        volume=symbol.lots(int(data.volume)),
        price_open=float(raw.price),  # type: ignore[attr-defined]
        sl=float(raw.stopLoss),  # type: ignore[attr-defined]
        tp=float(raw.takeProfit),  # type: ignore[attr-defined]
        time=int(data.openTimestamp) // 1000,
        magic=COPIER_MAGIC if data.label == COPIER_LABEL else 0,
        comment=str(data.comment),
    )


def position_live(raw: object, net_profit: float | None, money_digits: int) -> PositionLive:
    return PositionLive(
        ticket=int(raw.positionId),  # type: ignore[attr-defined]
        profit=net_profit,
        swap=money(int(raw.swap), money_digits),  # type: ignore[attr-defined]
    )


def order_type(cts_type: int, trade_side: int) -> OrderType | None:
    """MT5's number for a resting cTrader order; None for anything that is not one -- a market
    order in flight, or a position's own stop-loss/take-profit order."""
    buy = trade_side == _BUY
    if cts_type == _LIMIT:
        return OrderType.BUY_LIMIT if buy else OrderType.SELL_LIMIT
    if cts_type == _STOP:
        return OrderType.BUY_STOP if buy else OrderType.SELL_STOP
    if cts_type == _STOP_LIMIT:
        return OrderType.BUY_STOP_LIMIT if buy else OrderType.SELL_STOP_LIMIT
    return None


def pending_order(raw: object, symbol: SymbolInfo) -> PendingOrder | None:
    """One resting ``ProtoOAOrder``, or None if it is not a resting order.

    A cTrader stop-limit order has a trigger price and a slippage in points instead of MT5's
    second price; that slippage, beyond the trigger in the order's direction, is MT5's limit."""
    data = raw.tradeData  # type: ignore[attr-defined]
    kind = order_type(int(raw.orderType), int(data.tradeSide))  # type: ignore[attr-defined]
    if kind is None:
        return None
    stoplimit = 0.0
    if kind in (OrderType.BUY_LIMIT, OrderType.SELL_LIMIT):
        price = float(raw.limitPrice)  # type: ignore[attr-defined]
    else:
        price = float(raw.stopPrice)  # type: ignore[attr-defined]
    if kind.is_stop_limit:
        reach = int(raw.slippageInPoints) * 10 ** (-symbol.digits)  # type: ignore[attr-defined]
        stoplimit = round(
            price + reach if kind is OrderType.BUY_STOP_LIMIT else price - reach, symbol.digits
        )
    return PendingOrder(
        ticket=int(raw.orderId),  # type: ignore[attr-defined]
        symbol=symbol.name,
        type=kind,
        volume=symbol.lots(int(data.volume)),
        price_open=price,
        sl=float(raw.stopLoss),  # type: ignore[attr-defined]
        tp=float(raw.takeProfit),  # type: ignore[attr-defined]
        price_stoplimit=stoplimit,
        time_setup=int(data.openTimestamp) // 1000,
        magic=COPIER_MAGIC if data.label == COPIER_LABEL else 0,
        comment=str(data.comment),
    )
