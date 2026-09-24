"""The settings editor's logic, with no UI in it.

What is worth pinning here is not that a number parses but the decisions around it: that the
form cannot drift from the schema, that the fields which must not be editable stay that way,
that an edit never disturbs a value it was not asked to touch, and that a bad entry changes
nothing at all.
"""

from __future__ import annotations

import pytest

from mt5copier.config import NoStopPolicy, Role, SizingMode, TerminalConfig
from mt5copier.settings_edit import (
    EDITABLE,
    Kind,
    advisories,
    apply_edits,
    describe_changes,
    initial_values,
)

TID = "A" * 32


def make(**kwargs: object) -> TerminalConfig:
    return TerminalConfig(terminal_id=TID, label="Slave", role=Role.SLAVE, **kwargs)  # type: ignore[arg-type]


# --- the form and the schema cannot drift apart --------------------------------------------


def test_every_editable_field_exists_on_the_model() -> None:
    for spec in EDITABLE:
        assert spec.name in TerminalConfig.model_fields, spec.name


def test_a_choice_offers_every_member_of_its_enum_and_nothing_else() -> None:
    """A member missing from the form is a mode the user can no longer pick; one the enum does
    not have would fail validation on save."""
    expected = {"sizing_mode": set(SizingMode), "no_stop_policy": set(NoStopPolicy)}
    for spec in EDITABLE:
        if spec.kind is Kind.CHOICE:
            assert {v for _, v in spec.choices} == {m.value for m in expected[spec.name]}


@pytest.mark.parametrize(
    "name",
    [
        "invert",  # flipping it under live copies makes the ledger describe the opposite of reality
        "allow_live_trading",  # the demo-only guard's override; must not be one keypress away
        "role",
        "enabled",
        "terminal_id",
        "exe_path",
        "symbol_prefix",
        "symbol_suffix",
        "symbol_map",
    ],
)
def test_the_fields_that_must_not_be_edited_here_are_not(name: str) -> None:
    assert name not in {f.name for f in EDITABLE}
    with pytest.raises(ValueError, match="not editable"):
        apply_edits(make(), {name: "x"})


# --- an untouched form changes nothing ------------------------------------------------------


def test_submitting_the_form_untouched_is_not_a_change() -> None:
    cfg = make(max_lot=2.5, risk_percent=0.75, copy_pending=True)
    outcome = apply_edits(cfg, initial_values(cfg))
    assert outcome.ok
    assert outcome.changes == ()
    assert outcome.config == cfg


def test_a_value_that_does_not_survive_text_is_not_disturbed_by_editing_another_field() -> None:
    """One third does not round-trip through a text box. Re-parsing every field on every save
    would nudge it, and a risk setting changing by itself is exactly what an editor must not do."""
    third = 1 / 3
    cfg = make(risk_percent=third)
    raw = initial_values(cfg) | {"copy_pending": True}
    outcome = apply_edits(cfg, raw)
    assert outcome.config is not None
    assert outcome.config.risk_percent == third
    assert [c.name for c in outcome.changes] == ["copy_pending"]


def test_retyping_an_equal_value_is_not_a_change() -> None:
    cfg = make(risk_percent=0.5)
    outcome = apply_edits(cfg, {"risk_percent": "0.50"})
    assert outcome.ok
    assert outcome.changes == ()


# --- editing -------------------------------------------------------------------------------


def test_a_number_is_changed() -> None:
    outcome = apply_edits(make(), {"risk_percent": "0.25"})
    assert outcome.config is not None
    assert outcome.config.risk_percent == 0.25
    assert describe_changes(outcome.changes) == "risk_percent 1 -> 0.25"


def test_a_whole_number_field_takes_a_whole_number() -> None:
    outcome = apply_edits(make(), {"max_deviation_points": "35"})
    assert outcome.config is not None
    assert outcome.config.max_deviation_points == 35


def test_a_choice_is_changed_by_value() -> None:
    outcome = apply_edits(make(), {"sizing_mode": "fixed_cash_risk"})
    assert outcome.config is not None
    assert outcome.config.sizing_mode is SizingMode.FIXED_CASH_RISK


def test_a_switch_is_changed() -> None:
    outcome = apply_edits(make(), {"copy_pending": True, "copy_partials": False})
    assert outcome.config is not None
    assert outcome.config.copy_pending is True
    assert outcome.config.copy_partials is False
    assert describe_changes(outcome.changes) == "copy_pending off -> on, copy_partials on -> off"


def test_a_blank_max_lot_means_no_ceiling() -> None:
    outcome = apply_edits(make(max_lot=5.0), {"max_lot": "  "})
    assert outcome.config is not None
    assert outcome.config.max_lot is None
    assert describe_changes(outcome.changes) == "max_lot 5 -> none"


def test_a_blank_max_lot_that_was_already_blank_is_not_a_change() -> None:
    outcome = apply_edits(make(), {"max_lot": ""})
    assert outcome.ok
    assert outcome.changes == ()


def test_the_edit_starts_from_the_live_settings_not_from_when_the_form_opened() -> None:
    """A rescan or a role change can land while the form is open. Everything the form did not
    edit comes from the live terminal, so none of it is overwritten with stale values."""
    opened = make(risk_percent=1.0)
    raw = initial_values(opened) | {"copy_pending": True}
    live = opened.model_copy(update={"role": Role.MASTER, "last_login": 4242})
    outcome = apply_edits(live, raw)
    assert outcome.config is not None
    assert outcome.config.role is Role.MASTER
    assert outcome.config.last_login == 4242
    assert outcome.config.copy_pending is True


# --- refusing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("risk_percent", "0"),
        ("risk_percent", "-1"),
        ("risk_percent", "100.5"),
        ("risk_cash", "0"),
        ("commission_per_lot_round_turn", "-0.01"),
        ("slippage_buffer_points", "-1"),
        ("max_deviation_points", "-5"),
        ("no_stop_lot_multiple", "0"),
        ("max_lot", "0"),
        ("max_lot", "-1"),
    ],
)
def test_the_models_bounds_are_enforced(field: str, text: str) -> None:
    outcome = apply_edits(make(), {field: text})
    assert not outcome.ok
    assert field in outcome.errors


@pytest.mark.parametrize("text", ["inf", "-inf", "nan", "1e999", "Infinity"])
def test_a_non_finite_number_is_refused(text: str) -> None:
    """risk_cash has only a lower bound, so ``inf`` would sail through its check and mean an
    unbounded risk budget."""
    outcome = apply_edits(make(), {"risk_cash": text})
    assert not outcome.ok
    assert "risk_cash" in outcome.errors


@pytest.mark.parametrize(
    ("field", "text", "expected"),
    [
        ("risk_percent", "150", "must be at most 100"),
        ("risk_percent", "0", "must be greater than 0"),
        ("slippage_buffer_points", "-1", "must be at least 0"),
        ("commission_per_lot_round_turn", "-0.5", "must be at least 0"),
    ],
)
def test_a_bound_is_reported_with_its_number(field: str, text: str, expected: str) -> None:
    """The number is the only part that tells the user what to type instead. pydantic's own
    wording is long enough that a form row clips it off the end, and it prints 100 as 100.0."""
    assert apply_edits(make(), {field: text}).errors[field] == expected


def test_a_comma_decimal_gets_a_hint() -> None:
    outcome = apply_edits(make(), {"risk_percent": "0,5"})
    assert "dot" in outcome.errors["risk_percent"]


@pytest.mark.parametrize(
    ("field", "text", "fragment"),
    [
        ("risk_percent", "abc", "not a number"),
        ("risk_percent", "", "required"),
        ("max_deviation_points", "20.5", "whole number"),
        ("slippage_buffer_points", "ten", "whole number"),
    ],
)
def test_text_that_is_not_a_number_says_so(field: str, text: str, fragment: str) -> None:
    outcome = apply_edits(make(), {field: text})
    assert fragment in outcome.errors[field]


def test_a_choice_that_is_not_an_option_is_refused_against_its_own_field() -> None:
    outcome = apply_edits(make(), {"sizing_mode": "lot_multiplier"})
    assert not outcome.ok
    assert "sizing_mode" in outcome.errors


def test_every_problem_is_reported_in_one_go() -> None:
    """Revealing them one at a time would make fixing a form a series of failed saves."""
    outcome = apply_edits(
        make(), {"risk_percent": "abc", "max_deviation_points": "-5", "risk_cash": "0"}
    )
    assert set(outcome.errors) == {"risk_percent", "max_deviation_points", "risk_cash"}


def test_a_refused_edit_changes_nothing_and_returns_no_config() -> None:
    cfg = make()
    before = cfg.model_dump()
    outcome = apply_edits(cfg, {"risk_percent": "0.5", "risk_cash": "-1"})
    assert outcome.config is None
    assert outcome.changes == ()
    assert cfg.model_dump() == before, "the terminal being edited must be left exactly as it was"


# --- advisories ------------------------------------------------------------------------------


def test_enabling_the_no_stop_multiple_warns_that_the_copy_is_unprotected() -> None:
    outcome = apply_edits(make(), {"no_stop_policy": "lot_multiple", "no_stop_lot_multiple": "2"})
    (warning,) = advisories(outcome)
    assert "UNPROTECTED" in warning
    assert "2x" in warning


def test_raising_the_multiple_while_already_enabled_warns_again() -> None:
    cfg = make(no_stop_policy=NoStopPolicy.LOT_MULTIPLE)
    assert advisories(apply_edits(cfg, {"no_stop_lot_multiple": "5"}))


def test_no_stop_warning_is_silent_when_the_policy_skips() -> None:
    assert advisories(apply_edits(make(), {"no_stop_lot_multiple": "5"})) == []


def test_removing_the_lot_cap_warns() -> None:
    (warning,) = advisories(apply_edits(make(max_lot=3.0), {"max_lot": ""}))
    assert "max lot removed" in warning


def test_setting_or_leaving_the_cap_does_not_warn() -> None:
    assert advisories(apply_edits(make(), {"max_lot": "3"})) == []
    assert advisories(apply_edits(make(), {"max_lot": ""})) == []


def test_a_refused_edit_has_nothing_to_warn_about() -> None:
    assert advisories(apply_edits(make(), {"risk_percent": "0"})) == []
