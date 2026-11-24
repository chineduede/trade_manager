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
from mt5copier.protocol import ActionKind, TradeConstraint
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


def test_changing_a_held_rule_lets_go_of_its_hold(tmp_path: Path) -> None:
    rule = daily(limit=1000, unit="money")
    b = book(rule, tmp=tmp_path)
    b.verdict(KEY, snap(equity=99_100.0), NOW)
    assert b.verdict(KEY, snap(equity=99_500.0), NOW + 60).hold is not None

    raised = rule.model_copy(update={"params": {**rule.params, "limit": 5000}})
    b.doc.set_account(KEY, b.doc.for_account(KEY).model_copy(update={"rules": (raised,)}))
    assert b.verdict(KEY, snap(equity=99_500.0), NOW + 120).hold is None

    # Still past the new limit: it trips again at once.
    assert b.verdict(KEY, snap(equity=95_500.0), NOW + 180).hold is not None


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
    in_use: float | None = 0.0

    def margin_for(self, direction: Any, symbol: str, volume: float, price: float) -> float | None:
        return None if self.margin_per_lot is None else self.margin_per_lot * volume

    def margin_used(self) -> float | None:
        return self.in_use


def _margin_cap(amount: float, mode: str, rule: str = "") -> tuple[TradeConstraint, ...]:
    return (TradeConstraint("margin", amount, mode, rule),)


def test_under_the_margin_cap_the_trade_goes_as_sized() -> None:
    session = MarginSession()
    assert run(session, cmd(constraints=_margin_cap(5_000.0, "decline"))).ok
    assert session.sent[-1]["volume"] == 2.0


def test_over_the_cap_a_decline_sends_nothing() -> None:
    session = MarginSession()
    result = run(session, cmd(constraints=_margin_cap(1_500.0, "decline")))
    assert not result.ok
    assert (
        result.error
        == "A guard declined it: 2 lots on EURUSD: 2,000.00 margin needed, 1,500.00 allowed"
    )
    assert [n.action for n in result.guard] == ["declined"]
    assert session.sent == []


def test_over_the_cap_a_shrink_fits_it() -> None:
    session = MarginSession()
    result = run(session, cmd(constraints=_margin_cap(1_555.0, "shrink", "Margin per trade")))
    assert result.ok
    assert session.sent[-1]["volume"] == 1.55
    # The answer says what the guard did, so the app can show it: the log alone hid it.
    [note] = result.guard
    assert note.text() == (
        "Margin per trade shrunk it: from 2 to 1.55 lots: 2,000.00 margin needed, 1,555.00 allowed"
    )


def test_a_warning_sends_as_sized() -> None:
    session = MarginSession()
    result = run(session, cmd(constraints=_margin_cap(100.0, "warn")))
    assert result.ok and [n.action for n in result.guard] == ["warned"]
    assert session.sent[-1]["volume"] == 2.0


def test_a_margin_cap_waits_out_a_quote_that_has_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's price read is as tick-tolerant as the open's own. It runs before the send,
    so on a symbol selected moments ago it is the first quote anyone has asked for -- reading
    None once and declining would refuse a trade the open itself was about to price fine."""
    sleeps: list[float] = []
    monkeypatch.setattr("mt5copier.executor.time.sleep", sleeps.append)
    session = MarginSession()
    session.quote_sequence = [None, (1.08500, 1.08510)]
    result = run(session, cmd(constraints=_margin_cap(5_000.0, "decline")))
    assert result.ok, result.error
    assert sleeps == [2.0]
    assert session.sent[-1]["volume"] == 2.0


# --- the total margin cap ------------------------------------------------------------------------


def _total(mode: str, *before: TradeConstraint) -> object:
    """A command capped at 5,000 total margin, with any ``before`` constraints fit first."""
    total = TradeConstraint("total_margin", 5_000.0, mode, "Total margin in use")
    return cmd(constraints=(*before, total))


def test_the_total_counts_the_margin_already_in_use() -> None:
    # 2 lots need 2,000: alone they fit the 5,000 total, but 3,800 is in use already.
    session = MarginSession(in_use=3_800.0)
    result = run(session, _total("shrink"))
    assert result.ok
    assert session.sent[-1]["volume"] == 1.2
    [note] = result.guard
    assert note.text() == (
        "Total margin in use shrunk it: from 2 to 1.2 lots: 2,000.00 margin needed, "
        "1,200.00 left of the 5,000.00 total (3,800.00 in use)"
    )


def test_under_the_total_the_trade_goes_as_sized() -> None:
    session = MarginSession(in_use=1_000.0)
    result = run(session, _total("decline"))
    assert result.ok and result.guard == ()
    assert session.sent[-1]["volume"] == 2.0


def test_a_total_already_used_up_declines_even_a_shrink() -> None:
    session = MarginSession(in_use=5_200.0)
    result = run(session, _total("shrink"))
    assert not result.ok and session.sent == []
    assert result.error == (
        "Total margin in use declined it: 5,200.00 margin is already in use, at or over the "
        "5,000.00 total"
    )


def test_an_unreadable_margin_in_use_declines() -> None:
    session = MarginSession(in_use=None)
    result = run(session, _total("shrink"))
    assert not result.ok and "could not read the margin in use" in (result.error or "")


def test_both_caps_shrink_in_turn_and_both_are_said() -> None:
    # Per trade: 1,500 allowed -> 1.5 lots. Total: 1,000 left -> 1 lot.
    session = MarginSession(in_use=4_000.0)
    result = run(
        session,
        _total("shrink", TradeConstraint("margin", 1_500.0, "shrink", "Margin per trade")),
    )
    assert result.ok and session.sent[-1]["volume"] == 1.0
    assert [(n.rule, n.action) for n in result.guard] == [
        ("Margin per trade", "shrunk"),
        ("Total margin in use", "shrunk"),
    ]


def test_margin_that_cannot_be_priced_is_refused() -> None:
    session = MarginSession(margin_per_lot=None)
    assert not run(session, cmd(constraints=_margin_cap(5_000.0, "shrink"))).ok
    assert session.sent == []


def test_an_unknown_constraint_kind_is_declined_not_ignored() -> None:
    # A newer engine may know a limit this worker does not; the guard asked for it, so the
    # safe answer is no trade, saying which rule could not be enforced.
    session = MarginSession()
    hedge = TradeConstraint("exposure", 1.0, "decline", "No hedging")
    result = run(session, cmd(constraints=(hedge,)))
    assert not result.ok and session.sent == []
    assert "No hedging" in (result.error or "") and "cannot enforce" in (result.error or "")
