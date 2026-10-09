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
from ..sizing import SymbolFacts
from .proto import OpenApiModelMessages_pb2 as om

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
    min_volume: int = 0
    max_volume: int = 0
    step_volume: int = 0
    """The order volume bounds, in hundredths of a unit."""
    base_asset_id: int = 0
    quote_asset_id: int = 0

    def lots(self, volume: int) -> float:
        return volume / self.lot_size if self.lot_size else 0.0

    def volume(self, lots: float) -> int:
        """Lots as cTrader's hundredths of a unit. Rounded rather than truncated: the lots were
        already put on the symbol's step, so only float noise is left to remove."""
        return round(lots * self.lot_size)

    @property
    def point(self) -> float:
        return 10.0 ** (-self.digits)


def facts(symbol: SymbolInfo) -> SymbolFacts:
    """The symbol as :mod:`mt5copier.sizing` sees an MT5 one, in lots."""
    return SymbolFacts(
        name=symbol.name,
        volume_min=symbol.lots(symbol.min_volume),
        volume_max=symbol.lots(symbol.max_volume),
        volume_step=symbol.lots(symbol.step_volume),
        point=symbol.point,
        digits=symbol.digits,
    )


def relative(distance: float, digits: int) -> int:
    """A price distance as a relative stop or target: in 1/100000 of a price unit, and on the
    symbol's own digits, which the server insists on."""
    return round(round(abs(distance), digits) * 100_000)


def is_cross(symbol: SymbolInfo, deposit_asset_id: int) -> bool:
    """Whether valuing the symbol's moves in the account's currency needs other symbols' prices:
    it is quoted in neither that currency nor its own base (EURGBP on a USD account)."""
    return deposit_asset_id not in (symbol.quote_asset_id, symbol.base_asset_id)


def price_value(
    symbol: SymbolInfo, deposit_asset_id: int, rate: float, conversion: float | None = None
) -> float | None:
    """What a 1.0 move in price is worth on one lot, in the account's currency -- the number
    MT5's ``order_calc_profit`` gives and cTrader does not.

    One lot is ``lot_size / 100`` units of the base asset, so a 1.0 move is worth that many
    units of the **quote** asset. That is the answer when the account is kept in the quote
    asset (EURUSD or XAUUSD on a USD account). When it is kept in the base asset instead
    (USDJPY on a USD account), quote money becomes the account's by dividing by the symbol's
    own price, given as ``rate``. Anything else (EURGBP on a USD account) is a cross, valued
    through ``conversion`` -- one unit of the quote asset in the account's currency, from
    :func:`conversion_rate` -- or None without one.
    """
    units = symbol.lot_size / 100.0
    if symbol.quote_asset_id == deposit_asset_id:
        return units
    if symbol.base_asset_id == deposit_asset_id:
        return units / rate if rate > 0 else None
    return units * conversion if conversion else None


def loss_per_lot(
    symbol: SymbolInfo,
    deposit_asset_id: int,
    entry: float,
    sl: float,
    conversion: float | None = None,
) -> float | None:
    """What one lot loses between ``entry`` and ``sl``, in the account's currency. Converted
    at the stop's price, which is the rate in force when the loss is taken; a cross through
    ``conversion`` (see :func:`price_value`).

    The distance is rounded to the symbol's digits first: 1.1000 - 1.0990 is a hair over
    0.001 in floating point, which would size an exact one-lot risk down to 0.99."""
    value = price_value(symbol, deposit_asset_id, sl, conversion)
    return round(abs(entry - sl), symbol.digits) * value if value is not None else None


@dataclass(frozen=True, slots=True)
class Link:
    """One symbol of a conversion chain: cTrader's ``ProtoOALightSymbol``, reduced to what
    walking the chain needs."""

    symbol_id: int
    base_asset_id: int
    quote_asset_id: int


def conversion_rate(
    chain: tuple[Link, ...],
    from_asset_id: int,
    to_asset_id: int,
    spots: dict[int, tuple[float | None, float | None]],
) -> float | None:
    """What one unit of ``from_asset_id`` is worth in ``to_asset_id``, walking ``chain`` with
    the ``(bid, ask)`` prices in ``spots``. None if a link has no price or the chain does not
    lead there.

    Each link is crossed from whichever of its assets the walk stands on: from its base by
    multiplying by its price, from its quote by dividing. The price is the side that makes the
    rate larger -- the ask to multiply, the bid to divide -- because the rate values a loss,
    and a loss valued high sizes a trade small rather than over its risk. The links are taken
    as they connect, not in the order cTrader lists them."""
    rate = 1.0
    asset = from_asset_id
    left = list(chain)
    while asset != to_asset_id:
        link = next((x for x in left if asset in (x.base_asset_id, x.quote_asset_id)), None)
        if link is None:
            return None
        left.remove(link)
        bid, ask = spots.get(link.symbol_id, (None, None))
        if link.base_asset_id == asset:
            if not ask:
                return None
            rate *= ask
            asset = link.quote_asset_id
        else:
            if not bid:
                return None
            rate /= bid
            asset = link.base_asset_id
    return rate


def money(value: int, digits: int) -> float:
    """cTrader's money fields are integers scaled by the account's ``moneyDigits``."""
    return float(value) / float(10**digits)


def _direction(trade_side: int) -> PositionType:
    return PositionType.BUY if trade_side == _BUY else PositionType.SELL


def trade_side(direction: PositionType) -> om.ProtoOATradeSide:
    return om.BUY if direction is PositionType.BUY else om.SELL


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


def position_live(
    raw: object,
    net_profit: float | None,
    money_digits: int,
    spot: tuple[float | None, float | None] | None = None,
) -> PositionLive:
    """``spot`` is the symbol's ``(bid, ask)``. A position's current price is MT5's: the bid
    for a buy, the price it would close at, and the ask for a sell."""
    bid, ask = spot or (None, None)
    buy = int(raw.tradeData.tradeSide) == _BUY  # type: ignore[attr-defined]
    return PositionLive(
        ticket=int(raw.positionId),  # type: ignore[attr-defined]
        price_current=bid if buy else ask,
        profit=net_profit,
        swap=money(int(raw.swap), money_digits),  # type: ignore[attr-defined]
    )


def spot_price(raw: int) -> float:
    """Spot prices come in 1/100000 of a unit."""
    return raw / 100_000


def is_open(raw: object) -> bool:
    """Whether a ``ProtoOAPosition`` is a real open position, rather than the empty one cTrader
    creates for every resting order."""
    return int(raw.positionStatus) == om.POSITION_STATUS_OPEN  # type: ignore[attr-defined]


def is_resting(raw: object) -> bool:
    data = raw.tradeData  # type: ignore[attr-defined]
    return order_type(int(raw.orderType), int(data.tradeSide)) is not None  # type: ignore[attr-defined]


def order_ticket(raw: object) -> int:
    """The ticket a resting order is known by: the id of **the position it will become**,
    which cTrader gives it on placement, and not its own order id.

    The copier tells a master's filled order from a cancelled one by the order's ticket
    reappearing as a position's, which is how MT5 numbers them. A cTrader order and its
    position have different ids, so reporting the order by its position's makes a slave's
    fill hand over the same way. The order id is the fallback for an order with no position."""
    if raw.HasField("positionId") and int(raw.positionId):  # type: ignore[attr-defined]
        return int(raw.positionId)  # type: ignore[attr-defined]
    return int(raw.orderId)  # type: ignore[attr-defined]


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


def resting_type(kind: OrderType | None) -> om.ProtoOAOrderType | None:
    """cTrader's order type for placing one of MT5's: LIMIT or STOP, the side going separately.
    None for a stop-limit, which the copier never places, or anything that is not resting."""
    if kind in (OrderType.BUY_LIMIT, OrderType.SELL_LIMIT):
        return om.LIMIT
    if kind in (OrderType.BUY_STOP, OrderType.SELL_STOP):
        return om.STOP
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
        ticket=order_ticket(raw),
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
