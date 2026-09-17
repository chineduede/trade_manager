"""Turning an intended trade into a lot size. Pure, and deliberately so.

The arithmetic here decides how much money is at risk on every copied trade, which makes it
the most expensive thing in the project to get quietly wrong -- so none of it touches MT5. The
two values that genuinely require a broker are passed *in*:

* ``loss_per_lot`` -- what one lot would lose between entry and stop, from the slave terminal's
  own ``order_calc_profit``.
* :class:`SymbolFacts` -- the volume step, bounds and point size from ``symbol_info``.

Everything else is ours, and is unit-tested against exact expected lot sizes.

Why ``order_calc_profit`` rather than tick arithmetic, for whoever supplies that number: the
manual alternative is ``sl_distance * (trade_tick_value / trade_tick_size)``, and it has two
traps. ``SymbolInfo`` exposes ``trade_tick_value_profit`` *and* ``trade_tick_value_loss``,
which differ on some instruments -- risk sizing needs the loss side, and plain
``trade_tick_value`` (what ``zele-mt5`` reaches for) is the wrong field. It also gets
cross-currency conversion wrong unless you do the conversion yourself. Asking the terminal what
one lot would lose, in the account currency, sidesteps both.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .config import SizingMode, TerminalConfig
from .logging_setup import get_logger
from .protocol import PositionType

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SymbolFacts:
    """The parts of ``symbol_info`` that sizing depends on, on the *slave's* terminal."""

    name: str
    volume_min: float
    volume_max: float
    volume_step: float
    point: float
    digits: int = 5
    filling_mask: int = 0
    """``symbol_info.filling_mode`` -- a bitmask of permitted modes, not a mode. See
    :mod:`mt5copier.execution`."""


class SizingFailure(StrEnum):
    """Why a trade could not be sized. Every one of these means *do not send an order*."""

    BELOW_MINIMUM = "computed size is under the symbol's minimum volume"
    NO_RISK_DISTANCE = "one lot would lose nothing between entry and stop"
    UNUSABLE_SYMBOL_FACTS = "the symbol's volume step or bounds are unusable"
    NON_POSITIVE_BUDGET = "the risk budget is zero or negative"


@dataclass(frozen=True, slots=True)
class SizingResult:
    """A lot size, or a reason there isn't one."""

    lots: float | None = None
    failure: SizingFailure | None = None
    raw_lots: float = 0.0
    """Before quantization and clamping, for the log -- the gap between this and ``lots`` is
    usually what explains a surprising fill size."""
    capped_by: str | None = None
    """``"max_lot"`` or ``"volume_max"`` when a ceiling bound the result, else None."""

    @property
    def ok(self) -> bool:
        return self.lots is not None

    def describe(self) -> str:
        if self.failure is not None:
            return f"no size: {self.failure.value} (wanted {self.raw_lots:.4f})"
        cap = f", capped by {self.capped_by}" if self.capped_by else ""
        return f"{self.lots:g} lots (raw {self.raw_lots:.4f}{cap})"


def risk_budget(cfg: TerminalConfig, balance: float) -> float:
    """Money this slave is willing to lose on one trade, in its own account currency.

    Balance, not equity: the user ruled equity-proportional sizing out, and balance is the
    figure that does not move while a trade is open -- sizing two trades minutes apart should
    not give different answers because the first one is currently in profit.
    """
    if cfg.sizing_mode is SizingMode.FIXED_CASH_RISK:
        return cfg.risk_cash
    return balance * cfg.risk_percent / 100.0


def widen_for_slippage(
    entry: float,
    sl: float,
    direction: PositionType,
    slippage_points: int,
    point: float,
) -> tuple[float, float]:
    """Push entry and stop apart by the configured slippage, in the direction that costs money.

    Both fills can go against us and both errors run the same way: a worse entry and a worse
    stop each widen the real loss. Budgeting for it makes the configured risk a ceiling rather
    than a best case. Returns ``(entry, sl)`` to hand to ``order_calc_profit``.

    Zero slippage returns the inputs unchanged, which reproduces the naive calculation exactly.
    """
    if slippage_points <= 0 or point <= 0:
        return entry, sl
    shift = slippage_points * point
    if direction is PositionType.BUY:
        return entry + shift, sl - shift
    return entry - shift, sl + shift


def quantize_volume(raw: float, facts: SymbolFacts, max_lot: float | None = None) -> SizingResult:
    """Round a raw lot size down to the symbol's step, then apply the ceilings.

    **Rounds down, never up.** On a small account with a wide stop, ``volume_min`` can be worth
    several times the intended risk, so a size below the minimum is a refusal rather than a
    round-up -- and the caller must treat it as such.

    Uses ``(raw / step).to_integral_value(ROUND_DOWN) * step`` rather than
    ``Decimal.quantize(step)``. ``quantize`` honours only the step's *exponent*, so a
    ``volume_step`` of 0.05 would silently round to 0.01 granularity and emit lots like 0.07
    that the broker rejects -- the bug in ``zele-mt5``.

    Ceilings are applied **after** quantization, and the capped value is re-quantized in case
    ``max_lot`` is not itself a multiple of the step.
    """
    if facts.volume_step <= 0 or facts.volume_min <= 0 or facts.volume_max < facts.volume_min:
        return SizingResult(failure=SizingFailure.UNUSABLE_SYMBOL_FACTS, raw_lots=raw)
    if raw <= 0:
        return SizingResult(failure=SizingFailure.BELOW_MINIMUM, raw_lots=raw)

    try:
        stepped = _floor_to_step(raw, facts.volume_step)
    except (ArithmeticError, InvalidOperation, ValueError):
        return SizingResult(failure=SizingFailure.UNUSABLE_SYMBOL_FACTS, raw_lots=raw)

    if stepped < facts.volume_min:
        return SizingResult(failure=SizingFailure.BELOW_MINIMUM, raw_lots=raw)

    capped_by: str | None = None
    ceiling = facts.volume_max
    if max_lot is not None and max_lot < ceiling:
        ceiling, capped_by = max_lot, "max_lot"
    elif stepped > facts.volume_max:
        capped_by = "volume_max"

    if stepped > ceiling:
        if capped_by is None:
            capped_by = "volume_max"
        stepped = _floor_to_step(ceiling, facts.volume_step)
        if stepped < facts.volume_min:
            # A ceiling below one step cannot be honoured without rounding up past it.
            return SizingResult(failure=SizingFailure.BELOW_MINIMUM, raw_lots=raw)
    else:
        capped_by = None

    return SizingResult(lots=stepped, raw_lots=raw, capped_by=capped_by)


def _floor_to_step(value: float, step: float) -> float:
    d_step = Decimal(str(step))
    stepped = (Decimal(str(value)) / d_step).to_integral_value(rounding="ROUND_DOWN") * d_step
    return float(stepped)


def lots_for_risk(
    *,
    risk_amount: float,
    loss_per_lot: float,
    commission_per_lot: float,
    facts: SymbolFacts,
    max_lot: float | None = None,
) -> SizingResult:
    """Size from the slave's own risk budget.

    ``loss_per_lot`` is what one lot loses between the (slippage-widened) entry and stop, from
    ``order_calc_profit``, as a positive number. Commission is added to it rather than
    subtracted from the budget, which keeps the equation linear in lots -- costs scale with
    size, so folding them into the per-lot denominator is both correct and iteration-free::

        lots = risk_amount / (loss_per_lot + commission_per_lot_round_turn)

    Leaving commission out means every trade risks the configured amount *plus* costs, and the
    tighter the stop the worse the overshoot: on a ten-point stop, commission can dominate.
    """
    if risk_amount <= 0:
        return SizingResult(failure=SizingFailure.NON_POSITIVE_BUDGET)

    cost_per_lot = abs(loss_per_lot) + max(commission_per_lot, 0.0)
    if cost_per_lot <= 0:
        # Either the stop is at the entry or the terminal could not price it. Sizing by
        # dividing by ~zero would ask for an unbounded position.
        return SizingResult(failure=SizingFailure.NO_RISK_DISTANCE)

    return quantize_volume(risk_amount / cost_per_lot, facts, max_lot)


def lots_for_master_multiple(
    *,
    master_volume: float,
    multiple: float,
    facts: SymbolFacts,
    max_lot: float | None = None,
) -> SizingResult:
    """Size as a multiple of the master's own lots -- the no-stop path.

    The only place a slave inherits the master's sizing, because with no stop there is no risk
    distance to work from. The risk that results is not the slave's configured risk and is not
    knowable in advance; ``max_lot`` is the only bound that still applies, which is why it is
    worth setting on any slave that enables this.
    """
    return quantize_volume(master_volume * multiple, facts, max_lot)
