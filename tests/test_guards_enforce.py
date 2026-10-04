"""The guards acting: the verdict and its latches, the gate on new trades, and the caps."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_executor import FakeSession, cmd, run
from test_guards import KEY, NOW, at, daily, snap
from test_reconcile import pos, slave

from mt5copier.config import NoStopPolicy
from mt5copier.guards import AccountGuards, GuardBook, GuardRule, GuardsDoc
from mt5copier.protocol import ActionKind
from mt5copier.reconcile import GuardGate, SkipReason, reconcile_slave


def book(*rules: GuardRule, tmp: Path, initial: float = 100_000.0) -> GuardBook:
    doc = GuardsDoc(accounts={KEY: AccountGuards(initial_balance=initial, rules=rules)})
    return GuardBook(doc, state_path=tmp / "state.json")


# --- the verdict -------------------------------------------------------------------------------


def test_nothing_held_while_comfortable(tmp_path: Path) -> None:
    v = book(daily(limit=1000, unit="money"), tmp=tmp_path).verdict(KEY, snap(), NOW)
    assert v.hold is None and v.warnings == ()


def test_past_the_buffer_the_slave_is_held_back(tmp_path: Path) -> None:
    b = book(daily(limit=1000, unit="money"), tmp=tmp_path)
    v = b.verdict(KEY, snap(equity=99_150.0), NOW)
    assert v.hold is not None and "Daily loss at 85% of its limit" in v.hold


def test_a_daily_breach_holds_after_recovery_until_the_reset(tmp_path: Path) -> None:
    b = book(daily(limit=1000, unit="money"), tmp=tmp_path)
    b.verdict(KEY, snap(equity=99_100.0), NOW)
    v = b.verdict(KEY, snap(equity=100_000.0), NOW + 60)
    assert v.hold is not None and "held until the reset" in v.hold

    # Held across a restart, from disk.
    again = GuardBook(b.doc, state_path=tmp_path / "state.json")
    assert again.verdict(KEY, snap(), NOW + 120).hold is not None

    tomorrow = at(2026, 10, 6, 9)
    assert again.verdict(KEY, snap(), tomorrow).hold is None


def test_a_drawdown_breach_holds_until_cleared_by_hand(tmp_path: Path) -> None:
    rule = GuardRule(id="dd", type="max_drawdown", params={"limit": 1000, "unit": "money"})
    b = book(rule, tmp=tmp_path)
    b.verdict(KEY, snap(equity=99_000.0), NOW)
    assert b.verdict(KEY, snap(), at(2026, 11, 1, 9)).hold is not None, "no reset lifts it"
    assert b.report(KEY, snap(), NOW).rules[0].latched == "hand"
    assert b.clear_latch(KEY, "dd")
    assert b.verdict(KEY, snap(), NOW).hold is None


def test_floating_loss_lets_go_when_it_recovers(tmp_path: Path) -> None:
    rule = GuardRule(type="floating_loss", params={"limit": 500, "unit": "money"})
    b = book(rule, tmp=tmp_path)
    assert b.verdict(KEY, snap(positions=((1.0, -450.0),)), NOW).hold is not None
    assert b.verdict(KEY, snap(positions=((1.0, -100.0),)), NOW).hold is None


def test_a_rule_set_to_warn_warns_and_holds_nothing(tmp_path: Path) -> None:
    b = book(daily(limit=1000, unit="money", on_breach="warn"), tmp=tmp_path)
    v = b.verdict(KEY, snap(equity=99_000.0), NOW)
    assert v.hold is None and v.warnings
    assert b.report(KEY, snap(), NOW).rules[0].latched is None, "a warning is not held"


def test_closing_rules_hold_back_until_closing_exists(tmp_path: Path) -> None:
    b = book(daily(limit=1000, unit="money", on_breach="flatten"), tmp=tmp_path)
    assert b.verdict(KEY, snap(equity=99_100.0), NOW).hold is not None


def test_a_switched_off_set_does_nothing(tmp_path: Path) -> None:
    doc = GuardsDoc(
        accounts={KEY: AccountGuards(enabled=False, rules=(daily(limit=1000, unit="money"),))}
    )
    v = GuardBook(doc, tmp_path / "s.json").verdict(KEY, snap(equity=90_000.0), NOW)
    assert v.hold is None


def test_caps_and_the_stop_rule(tmp_path: Path) -> None:
    b = book(
        GuardRule(type="risk_per_trade", params={"limit": 1, "on_breach": "shrink"}),
        GuardRule(type="risk_per_trade", params={"limit": 300, "unit": "money"}),
        GuardRule(type="margin_per_trade", params={"limit": 10}),
        GuardRule(type="stop_required"),
        tmp=tmp_path,
    )
    v = b.verdict(KEY, snap(margin_free=50_000.0), NOW)
    assert v.max_risk is not None and (v.max_risk.amount, v.max_risk.mode) == (300.0, "decline")
    assert v.max_margin is not None and v.max_margin.amount == pytest.approx(5_000.0)
    assert v.stop_required == "Stop loss required"
    assert v.hold is None, "per-trade rules never hold the slave back"


# --- the gate ------------------------------------------------------------------------------------


def _open(gate: GuardGate, **kw: Any) -> SkipReason | None:
    actions = reconcile_slave([pos(**kw)], [], slave(**_slave_kw()), gate=gate)
    (action,) = actions
    assert action.kind is ActionKind.OPEN
    return action.skip_reason


def _slave_kw() -> dict[str, Any]:
    return {"no_stop_policy": NoStopPolicy.LOT_MULTIPLE}


def test_a_held_slave_declines_new_trades() -> None:
    assert _open(GuardGate(hold=True)) is SkipReason.GUARD_HOLDS


def test_a_trade_with_no_stop_is_declined_only_when_a_rule_requires_one() -> None:
    assert _open(GuardGate(needs_stop=True), sl=0.0) is SkipReason.GUARD_NEEDS_STOP
    assert _open(GuardGate(needs_stop=True), sl=1.08) is None
    assert _open(GuardGate(), sl=0.0) is None


def test_over_risk_declines_trades_sized_from_their_stop_only() -> None:
    assert _open(GuardGate(over_risk=True)) is SkipReason.GUARD_RISK
    assert _open(GuardGate(over_risk=True), sl=0.0) is None, "a lot multiple has no known risk"


def test_a_held_slave_still_closes_what_it_holds() -> None:
    from mt5copier.protocol import PositionType
    from mt5copier.reconcile import CopiedPosition

    held = CopiedPosition(
        terminal_id=slave().terminal_id,
        master_ticket=1,
        slave_symbol="EURUSD",
        type=PositionType.BUY,
        master_volume=1.0,
        synced_sl=1.08,
        synced_tp=0.0,
        slave_ticket=9,
        volume=1.0,
    )
    actions = reconcile_slave([], [held], slave(), gate=GuardGate(hold=True))
    assert [(a.kind, a.skip_reason) for a in actions] == [(ActionKind.CLOSE, None)]


# --- the margin cap ------------------------------------------------------------------------------


@dataclass
class MarginSession(FakeSession):
    margin_per_lot: float | None = 1_000.0

    def margin_for(self, direction: Any, symbol: str, volume: float, price: float) -> float | None:
        return None if self.margin_per_lot is None else self.margin_per_lot * volume


def test_under_the_margin_cap_the_trade_goes_as_sized() -> None:
    session = MarginSession()
    assert run(session, cmd(max_margin=5_000.0, margin_cap="decline")).ok
    assert session.sent[-1]["volume"] == 2.0


def test_over_the_cap_a_decline_sends_nothing() -> None:
    session = MarginSession()
    result = run(session, cmd(max_margin=1_500.0, margin_cap="decline"))
    assert not result.ok and "over the 1500.00 a guard allows" in (result.error or "")
    assert session.sent == []


def test_over_the_cap_a_shrink_fits_it() -> None:
    session = MarginSession()
    assert run(session, cmd(max_margin=1_555.0, margin_cap="shrink")).ok
    assert session.sent[-1]["volume"] == 1.55


def test_a_warning_sends_as_sized() -> None:
    session = MarginSession()
    assert run(session, cmd(max_margin=100.0, margin_cap="warn")).ok
    assert session.sent[-1]["volume"] == 2.0


def test_margin_that_cannot_be_priced_is_refused() -> None:
    session = MarginSession(margin_per_lot=None)
    assert not run(session, cmd(max_margin=5_000.0, margin_cap="shrink")).ok
    assert session.sent == []
