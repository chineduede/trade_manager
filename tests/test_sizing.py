"""Sizing tests.

This is the arithmetic that decides how much money is at risk on every copied trade, so the
tests assert exact lot sizes rather than ranges. Nothing here touches MT5: the two values that
need a broker (``loss_per_lot`` and :class:`SymbolFacts`) are inputs.
"""

from __future__ import annotations

import pytest

from mt5copier.config import SizingMode, TerminalConfig
from mt5copier.protocol import PositionType
from mt5copier.sizing import (
    SizingFailure,
    SymbolFacts,
    lots_for_master_multiple,
    lots_for_risk,
    quantize_volume,
    risk_budget,
    widen_for_slippage,
)

TID = "A" * 32


def facts(
    volume_min: float = 0.01,
    volume_max: float = 100.0,
    volume_step: float = 0.01,
    point: float = 0.00001,
) -> SymbolFacts:
    return SymbolFacts(
        name="EURUSD",
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        point=point,
    )


def slave(**kw: object) -> TerminalConfig:
    base: dict[str, object] = {"terminal_id": TID}
    base.update(kw)
    return TerminalConfig(**base)  # type: ignore[arg-type]


# --- the risk budget ------------------------------------------------------------------------


def test_percent_of_balance_uses_this_slaves_own_balance() -> None:
    assert risk_budget(slave(risk_percent=2.0), balance=50_000.0) == 1_000.0


def test_fixed_cash_ignores_the_balance_entirely() -> None:
    cfg = slave(sizing_mode=SizingMode.FIXED_CASH_RISK, risk_cash=250.0)
    assert risk_budget(cfg, balance=50_000.0) == 250.0
    assert risk_budget(cfg, balance=1_000_000.0) == 250.0


# --- slippage -------------------------------------------------------------------------------


def test_slippage_widens_a_buy_on_both_sides() -> None:
    """A worse entry and a worse stop fill both enlarge the loss, so both are budgeted for."""
    entry, sl = widen_for_slippage(1.08500, 1.08000, PositionType.BUY, 20, 0.00001)
    assert entry == pytest.approx(1.08520)
    assert sl == pytest.approx(1.07980)


def test_slippage_widens_a_sell_the_other_way() -> None:
    entry, sl = widen_for_slippage(1.08000, 1.08500, PositionType.SELL, 20, 0.00001)
    assert entry == pytest.approx(1.07980)
    assert sl == pytest.approx(1.08520)


def test_zero_slippage_reproduces_the_naive_calculation() -> None:
    assert widen_for_slippage(1.085, 1.080, PositionType.BUY, 0, 0.00001) == (1.085, 1.080)


# --- quantization ---------------------------------------------------------------------------


def test_volume_rounds_down_to_the_step() -> None:
    assert quantize_volume(0.678, facts()).lots == pytest.approx(0.67)


def test_a_coarse_step_is_honoured_not_just_its_decimal_places() -> None:
    """The zele-mt5 bug: Decimal.quantize(0.05) keeps 0.01 granularity and emits 0.07, which
    the broker rejects. The step itself has to divide the result."""
    result = quantize_volume(0.078, facts(volume_step=0.05, volume_min=0.05))
    assert result.lots == pytest.approx(0.05)

    for raw in (0.19, 0.24, 0.2999):
        lots = quantize_volume(raw, facts(volume_step=0.05, volume_min=0.05)).lots
        assert lots is not None
        assert round(lots / 0.05) == pytest.approx(lots / 0.05, abs=1e-9), f"{lots} off-step"


def test_a_size_below_the_minimum_is_refused_not_rounded_up() -> None:
    """On a small account with a wide stop, volume_min can be several times the intended
    risk. Rounding up is the dangerous default."""
    result = quantize_volume(0.004, facts(volume_min=0.01))
    assert not result.ok
    assert result.failure is SizingFailure.BELOW_MINIMUM


def test_exactly_the_minimum_is_allowed() -> None:
    assert quantize_volume(0.01, facts()).lots == pytest.approx(0.01)


def test_max_lot_caps_after_quantization() -> None:
    result = quantize_volume(5.0, facts(), max_lot=2.0)
    assert result.lots == pytest.approx(2.0)
    assert result.capped_by == "max_lot"


def test_a_max_lot_off_the_step_is_brought_back_onto_it() -> None:
    result = quantize_volume(5.0, facts(volume_step=0.05, volume_min=0.05), max_lot=0.17)
    assert result.lots == pytest.approx(0.15)
    assert result.capped_by == "max_lot"


def test_the_symbols_own_maximum_still_applies() -> None:
    result = quantize_volume(500.0, facts(volume_max=100.0))
    assert result.lots == pytest.approx(100.0)
    assert result.capped_by == "volume_max"


def test_a_max_lot_under_one_step_is_a_refusal() -> None:
    """Honouring it would mean rounding up past the ceiling the user set."""
    result = quantize_volume(1.0, facts(volume_min=0.1, volume_step=0.1), max_lot=0.05)
    assert result.failure is SizingFailure.BELOW_MINIMUM


def test_nonsense_symbol_facts_are_refused_rather_than_divided_by() -> None:
    assert quantize_volume(1.0, facts(volume_step=0.0)).failure is (
        SizingFailure.UNUSABLE_SYMBOL_FACTS
    )
    assert quantize_volume(1.0, facts(volume_min=0.0)).failure is (
        SizingFailure.UNUSABLE_SYMBOL_FACTS
    )


def test_an_uncapped_result_reports_no_cap() -> None:
    assert quantize_volume(1.0, facts(), max_lot=5.0).capped_by is None


# --- risk-based sizing ----------------------------------------------------------------------


def test_lots_are_the_budget_divided_by_what_one_lot_risks() -> None:
    """1% of 100k is 1000; a lot losing 500 between entry and stop means two lots."""
    result = lots_for_risk(
        risk_amount=1_000.0, loss_per_lot=500.0, commission_per_lot=0.0, facts=facts()
    )
    assert result.lots == pytest.approx(2.0)


def test_commission_comes_out_of_the_budget_before_lots_are_derived() -> None:
    """Otherwise every trade risks the configured amount *plus* costs."""
    plain = lots_for_risk(
        risk_amount=1_000.0, loss_per_lot=100.0, commission_per_lot=0.0, facts=facts()
    )
    costed = lots_for_risk(
        risk_amount=1_000.0, loss_per_lot=100.0, commission_per_lot=7.0, facts=facts()
    )
    assert plain.lots == pytest.approx(10.0)
    assert costed.lots == pytest.approx(9.34)  # 1000 / 107 = 9.345..., floored to the step
    assert costed.lots is not None and plain.lots is not None and costed.lots < plain.lots


def test_commission_dominates_when_the_stop_is_tight() -> None:
    """The case that makes leaving it out indefensible rather than merely imprecise."""
    tight = lots_for_risk(
        risk_amount=100.0, loss_per_lot=1.0, commission_per_lot=7.0, facts=facts()
    )
    assert tight.lots == pytest.approx(12.5)  # not 100 lots


def test_a_loss_per_lot_sign_does_not_matter() -> None:
    """order_calc_profit returns a negative number for a losing move."""
    negative = lots_for_risk(
        risk_amount=1_000.0, loss_per_lot=-500.0, commission_per_lot=0.0, facts=facts()
    )
    assert negative.lots == pytest.approx(2.0)


def test_a_zero_risk_distance_is_refused_not_divided_by() -> None:
    """Dividing by nothing would ask for an unbounded position."""
    result = lots_for_risk(
        risk_amount=1_000.0, loss_per_lot=0.0, commission_per_lot=0.0, facts=facts()
    )
    assert result.failure is SizingFailure.NO_RISK_DISTANCE


def test_a_non_positive_budget_is_refused() -> None:
    result = lots_for_risk(
        risk_amount=0.0, loss_per_lot=500.0, commission_per_lot=0.0, facts=facts()
    )
    assert result.failure is SizingFailure.NON_POSITIVE_BUDGET


def test_a_tiny_account_is_refused_rather_than_given_the_minimum() -> None:
    result = lots_for_risk(
        risk_amount=2.0, loss_per_lot=1_000.0, commission_per_lot=0.0, facts=facts()
    )
    assert result.failure is SizingFailure.BELOW_MINIMUM


def test_max_lot_applies_to_risk_sizing_too() -> None:
    result = lots_for_risk(
        risk_amount=10_000.0, loss_per_lot=100.0, commission_per_lot=0.0, facts=facts(), max_lot=3.0
    )
    assert result.lots == pytest.approx(3.0)


# --- the no-stop path -----------------------------------------------------------------------


def test_a_multiple_of_the_masters_lots() -> None:
    result = lots_for_master_multiple(master_volume=2.0, multiple=0.5, facts=facts())
    assert result.lots == pytest.approx(1.0)


def test_the_multiple_is_quantized_like_any_other_size() -> None:
    result = lots_for_master_multiple(
        master_volume=0.33, multiple=0.5, facts=facts(volume_step=0.05, volume_min=0.05)
    )
    assert result.lots == pytest.approx(0.15)


def test_max_lot_is_the_only_bound_left_on_an_unstopped_copy() -> None:
    result = lots_for_master_multiple(master_volume=50.0, multiple=1.0, facts=facts(), max_lot=1.0)
    assert result.lots == pytest.approx(1.0)
    assert result.capped_by == "max_lot"


def test_describe_explains_a_refusal() -> None:
    text = lots_for_risk(
        risk_amount=2.0, loss_per_lot=1_000.0, commission_per_lot=0.0, facts=facts()
    ).describe()
    assert "under the symbol's minimum" in text
