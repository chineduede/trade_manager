"""When a copy's stop moves to breakeven, and where to.

Pure, so the maths can be tested without a terminal. The engine calls :func:`breakeven_level`
for each copy on a slave with breakeven on, reading the slave's own position -- its fill and
its current price -- and records the answer on the copy as a floor. It never sends anything
itself: the diff sees the floor on the next tick and sends the ``MODIFY_SLTP`` through the
usual path (see :func:`mt5copier.reconcile.protective_sl`).

Everything is measured on the slave, not the master. A slave fills a little away from the
master and a reversed copy points the other way, and in both cases the copy's own fill is the
price whose loss a breakeven is meant to rule out.

**Where the stop goes** is entry plus a buffer, and the buffer is the slave's own costs rather
than a setting of its own: the price move that pays its round-turn commission, plus its
slippage buffer in points. A breakeven exit then really does come out at nothing lost. The
commission is the copy's own, read from its opening deal and doubled for the way out; the
slave's ``commission_per_lot_round_turn`` is the fallback for a copy whose deal showed none (a
resting order's fill, an older copy), and over-buffers a free symbol, which errs the safe way.

The spread needs no allowance of its own: a copy's fill is already on the far side of it (a buy
opens at the ask) and its stop is hit on the near side (a buy's at the bid), so a stop at entry
plus the buffer exits having paid it. **When** it goes is either a fraction of R or a price
set by hand on that one position.
"""

from __future__ import annotations

import math

from .config import BreakevenMode, TerminalConfig
from .protocol import PositionType
from .reconcile import CopiedPosition


def buffer_distance(copy: CopiedPosition, cfg: TerminalConfig) -> float | None:
    """How far past entry the stop goes, in price: commission plus slippage.

    The commission is per lot -- the copy's own from its fill, else the slave's
    ``commission_per_lot_round_turn`` -- divided by what a 1.0 price move is worth per lot, so
    the lot size cancels and the distance is the same for any size of copy. None when there is
    a commission but this copy's price value is unknown (it was recorded before that was kept):
    guessing would put the stop a loss away from where it should be.
    """
    slippage = cfg.slippage_buffer_points * copy.point
    commission = copy.commission_per_lot or cfg.commission_per_lot_round_turn
    if commission <= 0:
        return slippage
    if copy.price_value <= 0:
        return None
    return commission / copy.price_value + slippage


def breakeven_stop(copy: CopiedPosition, price_open: float, buffer: float) -> float:
    """Entry plus the buffer on the profitable side, rounded to the symbol's point *away* from
    entry, so the rounding never eats into what the buffer was there to pay for."""
    sign = 1.0 if copy.type is PositionType.BUY else -1.0
    raw = price_open + sign * buffer
    if copy.point <= 0:
        return raw
    steps = raw / copy.point
    # A hair of tolerance, so a level already on the grid is not pushed a whole point out.
    snapped = math.ceil(steps - 1e-6) if sign > 0 else math.floor(steps + 1e-6)
    return round(snapped * copy.point, copy.digits or 10)


def check_breakeven_price(
    copy: CopiedPosition, price_open: float, price: float, cfg: TerminalConfig
) -> str | None:
    """Why a hand-set breakeven price is unusable for this copy, or None if it is fine.

    It has to be past where the stop would go, on the profitable side; otherwise the trigger
    would fire with the stop at or beyond the price, which the broker refuses. A price the
    market has already passed is fine -- it triggers on the next tick.
    """
    if price <= 0:
        return "the price has to be above zero"
    buffer = buffer_distance(copy, cfg)
    if buffer is None:
        return (
            "this copy was opened before its commission could be priced, so its breakeven "
            "level is unknown"
        )
    stop = breakeven_stop(copy, price_open, buffer)
    beyond = price > stop if copy.type is PositionType.BUY else price < stop
    if not beyond:
        side = "above" if copy.type is PositionType.BUY else "below"
        return (
            f"for a {copy.type.name.lower()} it has to be {side} {stop:g} "
            "(entry plus commission and slippage)"
        )
    return None


def breakeven_level(
    copy: CopiedPosition, price_open: float, price_current: float, cfg: TerminalConfig
) -> float | None:
    """The stop to set if this copy has just crossed its trigger, else None.

    A breakeven price set by hand on the copy (``breakeven_at``) is the trigger whenever there
    is one, whether or not the slave has breakeven on. Otherwise it is the slave's R.

    None also covers every case where there is nothing sound to do: no trigger at all, the
    copy exempted by hand or already triggered, its point size or price value unknown (a copy
    recorded before those were kept), or an R trigger on a copy that opened with no stop and so
    has no R.

    ``price_current`` is MT5's own for the position: the bid for a buy and the ask for a sell,
    the price it would close at. A copy has to be past the buffer as well as the trigger, or
    the stop would land at or beyond the price and be refused -- a small R on a tight stop can
    be inside the commission.
    """
    if copy.breakeven_exempt or copy.breakeven_sl != 0.0 or copy.point <= 0.0:
        return None
    sign = 1.0 if copy.type is PositionType.BUY else -1.0
    gained = (price_current - price_open) * sign
    if copy.breakeven_at:
        threshold = (copy.breakeven_at - price_open) * sign
    elif cfg.breakeven_mode is BreakevenMode.R and copy.risk_distance > 0.0:
        threshold = cfg.breakeven_trigger_r * copy.risk_distance
    else:
        return None
    buffer = buffer_distance(copy, cfg)
    if buffer is None:
        return None
    stop = breakeven_stop(copy, price_open, buffer)
    # Half a point of slack: prices are floats, and a copy exactly on its trigger otherwise
    # misses it by a rounding error.
    slack = copy.point / 2
    if gained + slack < threshold or (price_current - stop) * sign <= slack:
        return None
    return stop
