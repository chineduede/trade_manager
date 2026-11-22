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
from .protocol import GuardNote, PositionType, TradeConstraint

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
    expiration_mask: int = 0
    """``symbol_info.expiration_mode`` -- which pending-order lifetimes the broker permits on
    this symbol, as a bitmask (:class:`mt5copier.execution.SymbolExpiration`). Not sizing, but
    read from the same ``symbol_info`` call everything else here comes from."""


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
        stepped = floor_to_step(raw, facts.volume_step)
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
        stepped = floor_to_step(ceiling, facts.volume_step)
        if stepped < facts.volume_min:
            # A ceiling below one step cannot be honoured without rounding up past it.
            return SizingResult(failure=SizingFailure.BELOW_MINIMUM, raw_lots=raw)
    else:
        capped_by = None

    return SizingResult(lots=stepped, raw_lots=raw, capped_by=capped_by)


@dataclass(frozen=True, slots=True)
class MarginFit:
    """What a guard's margin cap makes of one trade: the lots to send, or why it is refused,
    and what the guard did, to be said back with the answer."""

    lots: float | None
    """None: refused."""
    error: str = ""
    note: GuardNote | None = None


def fit_margin(
    *,
    lots: float,
    margin: float | None,
    cap: float,
    mode: str,
    symbol: str,
    facts: SymbolFacts,
    rule: str = "",
    rule_id: str = "",
    allowed: str = "",
) -> MarginFit:
    """The lots to send under a prop-firm guard's margin cap.

    Margin is linear in lots, so a trade over the cap shrinks by the ratio and is rounded down
    to the symbol's step -- never up, which would put it back over. One whose ``margin`` could
    not be priced is refused rather than sent unmeasured: the cap exists because a trade once
    went out at a size nobody meant. Set only to warn, the trade goes as sized either way.
    """
    rule = rule or "A guard"

    def note(action: str, detail: str) -> GuardNote:
        return GuardNote(rule, action, detail, rule_id)

    if margin is None:
        if mode == "warn":
            said = f"could not price the margin of {lots:g} lots on {symbol}; sent as sized"
            return MarginFit(lots, note=note("warned", said))
        why = f"could not price the margin of {lots:g} lots on {symbol}, which it limits"
        declined = note("declined", why)
        return MarginFit(None, declined.text(), declined)
    if margin <= cap:
        return MarginFit(lots)
    over = f"{margin:,.2f} margin needed, {allowed or f'{cap:,.2f} allowed'}"
    if mode == "warn":
        return MarginFit(lots, note=note("warned", f"{lots:g} lots sent as sized: {over}"))
    if mode == "shrink":
        fitted = floor_to_step(lots * cap / margin, facts.volume_step)
        if fitted >= facts.volume_min:
            return MarginFit(
                fitted, note=note("shrunk", f"from {lots:g} to {fitted:g} lots: {over}")
            )
        why = (
            f"even {facts.volume_min:g} lots on {symbol} needs more than the {cap:,.2f} margin "
            f"allowed ({lots:g} lots: {margin:,.2f})"
        )
        declined = note("declined", why)
        return MarginFit(None, declined.text(), declined)
    why = f"{lots:g} lots on {symbol}: {over}"
    declined = note("declined", why)
    return MarginFit(None, declined.text(), declined)


def fit_total_margin(
    *,
    lots: float,
    margin: float | None,
    total: float,
    in_use: float | None,
    mode: str,
    symbol: str,
    facts: SymbolFacts,
    rule: str = "",
    rule_id: str = "",
) -> MarginFit:
    """The lots to send under a cap on every open trade's margin and this one's together:
    :func:`fit_margin` against what is left of ``total`` once ``in_use`` is taken off."""
    if in_use is None:
        name = rule or "A guard"
        if mode == "warn":
            said = "could not read the margin in use; sent as sized"
            return MarginFit(lots, note=GuardNote(name, "warned", said, rule_id))
        note = GuardNote(name, "declined", "could not read the margin in use", rule_id)
        return MarginFit(None, note.text(), note)
    left = total - in_use
    if left <= 0 and mode != "warn":
        why = f"{in_use:,.2f} margin is already in use, at or over the {total:,.2f} total"
        note = GuardNote(rule or "A guard", "declined", why, rule_id)
        return MarginFit(None, note.text(), note)
    return fit_margin(
        lots=lots,
        margin=margin,
        cap=left,
        mode=mode,
        symbol=symbol,
        facts=facts,
        rule=rule,
        rule_id=rule_id,
        allowed=f"{max(left, 0.0):,.2f} left of the {total:,.2f} total ({in_use:,.2f} in use)",
    )


def fit_lots(
    *,
    lots: float,
    cap: float,
    mode: str,
    symbol: str,
    facts: SymbolFacts,
    rule: str = "",
    rule_id: str = "",
    allowed: str = "",
) -> MarginFit:
    """The lots to send under the asset-class guard's lots ceiling.

    The engine already reduced the class table to one number, so this is the plain half of
    :func:`fit_margin`: nothing to price, just a ceiling in the trade's own unit. ``allowed``
    is the engine's reading -- it names the class, so it is said back wherever the trade is
    shown."""
    rule = rule or "A guard"

    def note(action: str, detail: str) -> GuardNote:
        return GuardNote(rule, action, detail, rule_id)

    if lots <= cap:
        return MarginFit(lots)
    over = allowed or f"capped at {cap:g} lots"
    if mode == "warn":
        return MarginFit(lots, note=note("warned", f"{lots:g} lots sent as sized: {over}"))
    if mode == "shrink":
        fitted = floor_to_step(cap, facts.volume_step)
        if fitted >= facts.volume_min:
            return MarginFit(
                fitted, note=note("shrunk", f"from {lots:g} to {fitted:g} lots: {over}")
            )
        why = f"even {facts.volume_min:g} lots on {symbol} is over the cap: {over}"
        declined = note("declined", why)
        return MarginFit(None, declined.text(), declined)
    why = f"{lots:g} lots on {symbol}: {over}"
    declined = note("declined", why)
    return MarginFit(None, declined.text(), declined)


MARGIN_KINDS = frozenset({"margin", "total_margin"})
"""The constraint kinds whose fit needs the trade's margin priced by the broker --
``total_margin`` also needs the margin in use, read at send time."""


def fit_constraint(
    constraint: TradeConstraint,
    symbol: str,
    facts: SymbolFacts,
    lots: float,
    *,
    margin: float | None = None,
    in_use: float | None = None,
) -> MarginFit:
    """One :class:`~mt5copier.protocol.TradeConstraint` applied: the dispatch both brokers'
    fit loops share. ``margin`` and ``in_use`` are the broker reads the :data:`MARGIN_KINDS`
    need; the ``lots`` kind needs none. A kind this build does not know is declined, never
    ignored -- the guard asked for a limit, and sending unmeasured is the unsafe side."""
    if constraint.kind == "lots":
        return fit_lots(
            lots=lots,
            cap=constraint.cap,
            mode=constraint.mode,
            symbol=symbol,
            facts=facts,
            rule=constraint.rule,
            rule_id=constraint.rule_id,
            allowed=constraint.allowed,
        )
    if constraint.kind == "margin":
        return fit_margin(
            lots=lots,
            margin=margin,
            cap=constraint.cap,
            mode=constraint.mode,
            symbol=symbol,
            facts=facts,
            rule=constraint.rule,
            rule_id=constraint.rule_id,
        )
    if constraint.kind == "total_margin":
        return fit_total_margin(
            lots=lots,
            margin=margin,
            total=constraint.cap,
            in_use=in_use,
            mode=constraint.mode,
            symbol=symbol,
            facts=facts,
            rule=constraint.rule,
            rule_id=constraint.rule_id,
        )
    note = GuardNote(
        constraint.rule or "A guard",
        "declined",
        f"this build cannot enforce its {constraint.kind!r} limit",
        constraint.rule_id,
    )
    return MarginFit(None, note.text(), note)


def floor_to_step(value: float, step: float) -> float:
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
