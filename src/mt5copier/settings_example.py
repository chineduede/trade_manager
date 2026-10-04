"""The worked example under the settings form: what this slave would open on one of the master's
real trades, with the settings as they stand in the form. No UI in it, so it can be tested.

The arithmetic is the sizing code's own (:func:`~mt5copier.sizing.risk_budget` and
:func:`~mt5copier.sizing.widen_for_slippage`), so the example cannot drift from what is sent.
What it cannot have is the one figure only the slave's terminal knows -- what a price move is
worth on one lot. That is taken from a fill this slave already made on the same symbol (a copy
it holds, or one it opened this session), and when there is none, the example says what it
would divide by instead of guessing a number.

It also cannot know the symbol's lot step, so the lots it shows are before the broker's
rounding, and are labelled "about".
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from .config import SizingMode, TerminalConfig
from .protocol import PositionType
from .sizing import risk_budget, widen_for_slippage


@dataclass(frozen=True, slots=True)
class MasterTrade:
    """One of the master's trades, as the example needs it."""

    ticket: int
    symbol: str
    buy: bool
    entry: float
    sl: float = 0.0
    tp: float = 0.0


@dataclass(frozen=True, slots=True)
class PriceValue:
    """What this slave's terminal said a 1.0 price move is worth on one lot of a symbol, at a
    fill. ``point`` turns the slippage setting into a price."""

    value: float
    point: float = 0.0


@dataclass(frozen=True, slots=True)
class Example:
    lead: str
    """``Master buys BTCUSD with its stop 300.00 away.``"""

    working: str
    """``975.13 risk ÷ (300.00 stop + 3.50 commission) =``, or the same with words for what is
    not known."""

    result: str = ""
    """``this slave buys about 3.21 lots``; blank when there is no figure."""

    note: str = ""
    """Why there is no figure, or what bounded it."""


def pick(
    trades: Iterable[MasterTrade],
    fills: dict[int, PriceValue],
    symbols: dict[int, str],
    invert: bool,
) -> tuple[MasterTrade | None, PriceValue | None]:
    """The trade to work the example on, newest first, and the price value to work it with.

    ``trades`` come newest first. ``fills`` is this slave's price value by master ticket, and
    ``symbols`` the master symbol each of those tickets was, so that a fill on one EURUSD trade
    prices every EURUSD trade. A trade this slave can price is preferred to a newer one it
    cannot. Only trades with the stop the slave sizes from count: the master's stop, or its
    target on a reversed slave.
    """
    by_symbol: dict[str, PriceValue] = {}
    for ticket, pv in fills.items():
        symbol = symbols.get(ticket)
        if symbol and pv.value > 0:
            by_symbol.setdefault(symbol, pv)
    usable = [t for t in trades if (t.tp if invert else t.sl) > 0]
    for trade in usable:
        if trade.symbol in by_symbol:
            return trade, by_symbol[trade.symbol]
    return (usable[0], None) if usable else (None, None)


def _price(value: float, point: float) -> str:
    if point > 0:
        digits = max(0, -math.floor(math.log10(point) + 1e-9))
        return f"{value:.{digits}f}"
    return f"{round(value, 8):g}"


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _floor2(value: float) -> float:
    return math.floor(value * 100 + 1e-9) / 100


def work(
    cfg: TerminalConfig,
    balance: float | None,
    trade: MasterTrade | None,
    price: PriceValue | None,
) -> Example:
    """The example for ``cfg``, which holds what the form says now, not what is saved."""
    commission = max(cfg.commission_per_lot_round_turn, 0.0)
    if cfg.sizing_mode is SizingMode.PERCENT_OF_BALANCE and balance is None:
        risk_text, risk = f"{cfg.risk_percent:g}% of a balance not read yet", None
    else:
        risk = risk_budget(cfg, balance or 0.0)
        risk_text = _money(risk)

    if trade is None:
        stop = "target" if cfg.invert else "stop"
        return Example(
            lead=f"The master has no open trade with a {stop} to work an example on.",
            working=f"{risk_text} risk ÷ (what one lot loses at the stop + "
            f"{_money(commission)} commission)",
        )

    point = price.point if price else 0.0
    edge = trade.tp if cfg.invert else trade.sl
    distance = abs(trade.entry - edge)
    verb = "buys" if trade.buy else "sells"
    if cfg.invert:
        mine = "sells" if trade.buy else "buys"
        lead = (
            f"Master {verb} {trade.symbol} with its target {_price(distance, point)} away; "
            f"reversed, this slave {mine} with its stop there."
        )
    else:
        mine = verb
        lead = f"Master {verb} {trade.symbol} with its stop {_price(distance, point)} away."

    if price is None:
        return Example(
            lead=lead,
            working=f"{risk_text} risk ÷ (what one lot loses over {_price(distance, point)} + "
            f"{_money(commission)} commission)",
            note=f"Lots show once this slave has filled a {trade.symbol} trade.",
        )

    buying = trade.buy != cfg.invert
    direction = PositionType.BUY if buying else PositionType.SELL
    stop_at = edge
    wide_entry, wide_stop = widen_for_slippage(
        trade.entry, stop_at, direction, cfg.slippage_buffer_points, point
    )
    at_stop = distance * price.value
    slipped = abs(wide_entry - wide_stop) * price.value - at_stop
    parts = [f"{_money(at_stop)} stop"]
    if slipped > 0.005:
        parts.append(f"{_money(slipped)} slippage")
    parts.append(f"{_money(commission)} commission")
    working = f"{risk_text} risk ÷ ({' + '.join(parts)}) ="
    if risk is None:
        return Example(lead=lead, working=working, note="Lots show once the balance is read.")

    per_lot = at_stop + slipped + commission
    if risk <= 0 or per_lot <= 0:
        return Example(lead=lead, working=working, result="no trade", note="nothing to risk")
    lots = risk / per_lot
    if cfg.max_lot is not None and lots > cfg.max_lot:
        return Example(
            lead=lead,
            working=working,
            result=f"this slave {mine} {cfg.max_lot:g} lots",
            note=f"{_floor2(lots):g} lots, capped at the max lot",
        )
    if _floor2(lots) <= 0:
        return Example(
            lead=lead,
            working=working,
            result=f"this slave {mine} {lots:.4f} lots",
            note="under 0.01 lots, which most brokers refuse",
        )
    return Example(
        lead=lead,
        working=working,
        result=f"this slave {mine} about {_floor2(lots):g} lots",
        note="before rounding down to the broker's lot step",
    )
