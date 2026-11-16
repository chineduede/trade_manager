"""The settings editor's logic, with no UI in it.

What is worth pinning here is not that a number parses but the decisions around it: that the
form cannot drift from the schema, that the fields which must not be editable stay that way,
that an edit never disturbs a value it was not asked to touch, and that a bad entry changes
nothing at all.
"""

from __future__ import annotations

import pytest

from mt5copier.config import (
    AppConfig,
    BreakevenMode,
    NoStopPolicy,
    Role,
    SizingMode,
    TerminalConfig,
    account_key,
)
from mt5copier.settings_edit import (
    EDITABLE,
    MASTER_FIELDS,
    Kind,
    advisories,
    apply_edits,
    copies_word,
    describe_changes,
    initial_values,
    live_override_phrase,
    rename,
    set_live_override,
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
    expected = {
        "sizing_mode": set(SizingMode),
        "no_stop_policy": set(NoStopPolicy),
        "breakeven_mode": set(BreakevenMode),
    }
    for spec in EDITABLE:
        if spec.kind is Kind.CHOICE:
            assert {v for _, v in spec.choices} == {m.value for m in expected[spec.name]}


NOT_EDITABLE = {
    "allow_live_trading",  # the demo-only guard's override: own key, typed confirmation
    "label",  # discovery overwrites it on every rescan, so an edit would silently revert
    "role",  # has its own keys, and set_role is where the one-master rule lives
    "terminal_id",
    "exe_path",
    "last_login",
    "last_server",
    "enabled",  # no longer offered: pausing replaced it, and it stays only so old files load
    "paused",  # a switch on the slave's row, flipped far more often than any setting
    "ctrader_account_id",  # which cTrader account it is: set by the login, an identity
    "ctrader_live",  # what the login says the account is
}


@pytest.mark.parametrize("name", sorted(NOT_EDITABLE))
def test_the_fields_that_must_not_be_edited_here_are_not(name: str) -> None:
    assert name not in {f.name for f in EDITABLE}
    with pytest.raises(ValueError, match="not editable"):
        apply_edits(make(), {name: "x"})


def test_every_setting_is_either_editable_or_deliberately_not() -> None:
    """A field added to the model must force a decision about the form, not be forgotten by it.
    If this fails, either put the new field in a section or in NOT_EDITABLE with the reason."""
    editable = {f.name for f in (*EDITABLE, *MASTER_FIELDS)}
    assert editable.isdisjoint(NOT_EDITABLE)
    assert editable | NOT_EDITABLE == set(TerminalConfig.model_fields)


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
    outcome = apply_edits(make(), {"copy_pending": True, "master_manages": False})
    assert outcome.config is not None
    assert outcome.config.copy_pending is True
    assert outcome.config.master_manages is False
    assert describe_changes(outcome.changes) == "copy_pending off -> on, master_manages on -> off"


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


# --- what an edit does to open copies is not decided here ----------------------------------------
#
# ``invert`` used to be refused under an open copy and ``enabled`` used to warn about one. Neither
# is judged in this module any more: an edit to a terminal that holds copies gives them up, for
# every setting alike, after the person has been asked (Engine.apply_settings and the UI).


def test_invert_is_an_ordinary_edit() -> None:
    outcome = apply_edits(make(), {"invert": True})
    assert outcome.config is not None
    assert outcome.config.invert is True
    assert describe_changes(outcome.changes) == "invert off -> on"


def test_turning_invert_off_is_an_ordinary_edit_too() -> None:
    outcome = apply_edits(make(invert=True), {"invert": False})
    assert outcome.ok
    assert describe_changes(outcome.changes) == "invert on -> off"


@pytest.mark.parametrize(("count", "text"), [(1, "1 open copy"), (2, "2 open copies")])
def test_copies_are_counted_in_the_singular_and_the_plural(count: int, text: str) -> None:
    assert copies_word(count) == text


def test_no_help_text_still_promises_a_refusal_or_a_resume() -> None:
    """Both were true once. ``invert`` was refused under a copy, and a disabled slave's copies were
    picked back up on re-enable; now the copies are given up, and the help must not say otherwise."""
    helps = {f.name: f.help.lower() for f in EDITABLE}
    assert "refused" not in helps["invert"]


# --- symbol prefix and suffix -------------------------------------------------------------------


def test_a_suffix_is_set() -> None:
    outcome = apply_edits(make(), {"symbol_suffix": "p"})
    assert outcome.config is not None
    assert outcome.config.symbol_suffix == "p"
    assert describe_changes(outcome.changes) == "symbol_suffix '' -> 'p'"


def test_a_suffix_can_be_cleared() -> None:
    outcome = apply_edits(make(symbol_suffix="p"), {"symbol_suffix": ""})
    assert outcome.config is not None
    assert outcome.config.symbol_suffix == ""


def test_surrounding_whitespace_on_a_suffix_is_dropped_but_inner_is_refused() -> None:
    """A stray space at the end of a pasted suffix would make every name miss, and the miss would
    only show as a refusal at trade time."""
    assert apply_edits(make(), {"symbol_suffix": " p "}).config.symbol_suffix == "p"  # type: ignore[union-attr]
    assert "spaces" in apply_edits(make(), {"symbol_prefix": "a b"}).errors["symbol_prefix"]


def test_the_suffix_keeps_the_brokers_own_casing() -> None:
    assert apply_edits(make(), {"symbol_suffix": ".Pro"}).config.symbol_suffix == ".Pro"  # type: ignore[union-attr]


# --- the symbol map -------------------------------------------------------------------------------


def test_a_map_is_read_one_pair_per_line() -> None:
    outcome = apply_edits(make(), {"symbol_map": "US30=US30.cash\nnas100=USTEC"})
    assert outcome.config is not None
    assert outcome.config.symbol_map == {"US30": "US30.cash", "NAS100": "USTEC"}


def test_a_map_also_takes_commas_and_spaces_around_the_equals() -> None:
    outcome = apply_edits(
        make(), {"symbol_map": "US30 = US30.cash,  GOLD =XAUUSDp ; NAS100= USTEC"}
    )
    assert outcome.config is not None
    assert outcome.config.symbol_map == {
        "US30": "US30.cash",
        "GOLD": "XAUUSDp",
        "NAS100": "USTEC",
    }


def test_the_target_keeps_its_casing_and_the_key_is_folded() -> None:
    outcome = apply_edits(make(), {"symbol_map": "gold=XAUUSDp"})
    assert outcome.config is not None
    assert outcome.config.symbol_map == {"GOLD": "XAUUSDp"}


@pytest.mark.parametrize("text", ["GOLD", "GOLD=", "=XAUUSDp", "GOLD=X=Y", "GOLD XAUUSDp"])
def test_an_entry_that_is_not_a_pair_is_refused(text: str) -> None:
    outcome = apply_edits(make(), {"symbol_map": text})
    assert not outcome.ok
    assert "MASTER=BROKER" in outcome.errors["symbol_map"]


def test_two_entries_for_one_symbol_that_disagree_are_refused() -> None:
    """Choosing between them would be a guess about which instrument was meant, the one thing
    symbol resolution must never do. The model refuses it on load; the form refuses it at entry."""
    outcome = apply_edits(make(), {"symbol_map": "GOLD=XAUUSDp\ngold=GOLD.a"})
    assert not outcome.ok
    assert "both" in outcome.errors["symbol_map"]


def test_two_entries_for_one_symbol_that_agree_are_one() -> None:
    outcome = apply_edits(make(), {"symbol_map": "GOLD=XAUUSDp\ngold=XAUUSDp"})
    assert outcome.config is not None
    assert outcome.config.symbol_map == {"GOLD": "XAUUSDp"}


def test_clearing_the_map_empties_it() -> None:
    outcome = apply_edits(make(symbol_map={"GOLD": "XAUUSDp"}), {"symbol_map": ""})
    assert outcome.config is not None
    assert outcome.config.symbol_map == {}


def test_reformatting_a_map_is_not_a_change() -> None:
    """Same pairs, written differently. Nothing should be saved or logged for it."""
    cfg = make(symbol_map={"GOLD": "XAUUSDp", "US30": "US30.cash"})
    outcome = apply_edits(cfg, {"symbol_map": "US30 = US30.cash, gold=XAUUSDp"})
    assert outcome.ok
    assert outcome.changes == ()


def test_the_map_starts_sorted_and_round_trips() -> None:
    cfg = make(symbol_map={"US30": "US30.cash", "GOLD": "XAUUSDp"})
    assert initial_values(cfg)["symbol_map"] == "GOLD=XAUUSDp\nUS30=US30.cash"
    outcome = apply_edits(cfg, initial_values(cfg))
    assert outcome.ok
    assert outcome.changes == ()


# --- the symbol filters ---------------------------------------------------------------------------


def test_a_whitelist_is_read_from_commas_spaces_or_lines() -> None:
    outcome = apply_edits(make(), {"symbol_whitelist": "EURUSD, XAUUSD  GBPUSD\nUS30"})
    assert outcome.config is not None
    assert outcome.config.symbol_whitelist == ["EURUSD", "XAUUSD", "GBPUSD", "US30"]


def test_a_filter_drops_repeats_ignoring_case_and_keeps_the_first_spelling() -> None:
    outcome = apply_edits(make(), {"symbol_blacklist": "EURUSD, eurusd, XAUUSD"})
    assert outcome.config is not None
    assert outcome.config.symbol_blacklist == ["EURUSD", "XAUUSD"]


def test_clearing_a_filter_empties_it() -> None:
    outcome = apply_edits(make(symbol_whitelist=["EURUSD"]), {"symbol_whitelist": ""})
    assert outcome.config is not None
    assert outcome.config.symbol_whitelist == []
    assert describe_changes(outcome.changes) == "symbol_whitelist EURUSD -> none"


def test_a_mapping_pasted_into_a_filter_is_refused() -> None:
    outcome = apply_edits(make(), {"symbol_whitelist": "GOLD=XAUUSDp"})
    assert "symbol map" in outcome.errors["symbol_whitelist"]


def test_a_filter_starts_as_a_comma_list_and_round_trips() -> None:
    cfg = make(symbol_blacklist=["EURUSD", "XAUUSD"])
    assert initial_values(cfg)["symbol_blacklist"] == "EURUSD, XAUUSD"
    assert apply_edits(cfg, initial_values(cfg)).changes == ()


# --- describing a change ----------------------------------------------------------------------------


def test_a_long_description_is_cut_for_a_notification_but_not_for_the_log() -> None:
    pairs = "\n".join(f"SYM{i}=BROKER{i}" for i in range(30))
    outcome = apply_edits(make(), {"symbol_map": pairs})
    full = describe_changes(outcome.changes)
    short = describe_changes(outcome.changes, limit=80)
    assert "SYM29=BROKER29" in full
    assert len(short) == 80
    assert short.endswith("...")


# --- the demo-only guard's override ---------------------------------------------------------------
#
# Off is the safe direction and takes nothing. On has to be asked for by name, on a slave, and a
# caller that forgets to ask must be refused rather than waved through.


def test_the_phrase_is_the_terminals_name() -> None:
    assert live_override_phrase(make()) == "Slave"


@pytest.mark.parametrize("label", ["", "   "])
def test_a_terminal_with_no_name_is_confirmed_by_the_first_eight_characters_of_its_id(
    label: str,
) -> None:
    cfg = TerminalConfig(terminal_id=TID, label=label, role=Role.SLAVE)
    assert live_override_phrase(cfg) == "AAAAAAAA"


def test_the_override_is_turned_on_by_typing_the_name() -> None:
    cfg = make(risk_percent=0.4, copy_pending=True)
    outcome = set_live_override(cfg, True, typed="Slave")
    assert outcome.ok
    assert outcome.config is not None
    assert outcome.config.allow_live_trading is True
    assert [(c.name, c.old, c.new) for c in outcome.changes] == [
        ("allow_live_trading", False, True)
    ]
    assert outcome.config.model_dump() == {**cfg.model_dump(), "allow_live_trading": True}, (
        "nothing else about the terminal is touched"
    )
    assert cfg.allow_live_trading is False, "the live object is not edited in place"


def test_turning_the_override_off_over_copies_on_a_real_account_warns() -> None:
    outcome = set_live_override(make(allow_live_trading=True), False)
    assert outcome.ok
    (warning,) = advisories(outcome, copies_on_real_account=2)
    assert "2 open copies on a real account" in warning
    assert "refuse to close or move them" in warning
    assert advisories(outcome) == [], "nothing held there, nothing to say"


@pytest.mark.parametrize("typed", ["slave", "  Slave  ", "SLAVE"])
def test_case_and_surrounding_space_do_not_matter(typed: str) -> None:
    assert set_live_override(make(), True, typed=typed).ok


@pytest.mark.parametrize("typed", [None, "", "   ", "Slav", "Slave2", "yes", "LIVE", "Slave Slave"])
def test_anything_but_the_name_refuses_and_changes_nothing(typed: str | None) -> None:
    """``None`` is "the caller did not ask". A guard that passes when nobody asked is not one."""
    outcome = set_live_override(make(), True, typed=typed)
    assert not outcome.ok
    assert outcome.config is None
    assert outcome.changes == ()
    assert "Slave" in outcome.errors["allow_live_trading"], "it says what to type"


def test_forgetting_to_ask_at_all_refuses() -> None:
    assert not set_live_override(make(), True).ok


@pytest.mark.parametrize("role", [Role.MASTER, Role.UNASSIGNED])
def test_only_a_slave_can_have_the_guard_lifted(role: Role) -> None:
    """``set_role`` leaves the flag alone, so one lifted on a terminal that is not a slave would
    ride along, unseen, on the day it is made one -- even with the right name typed."""
    cfg = TerminalConfig(terminal_id=TID, label="Slave", role=role)
    outcome = set_live_override(cfg, True, typed="Slave")
    assert not outcome.ok
    assert "slave" in outcome.errors["allow_live_trading"]


@pytest.mark.parametrize("role", [Role.SLAVE, Role.MASTER, Role.UNASSIGNED])
def test_turning_it_off_takes_nothing_and_works_whatever_the_role(role: Role) -> None:
    """The safe direction. A terminal whose role changed with the guard still lifted must be
    able to have it put back."""
    cfg = TerminalConfig(terminal_id=TID, label="Slave", role=role, allow_live_trading=True)
    outcome = set_live_override(cfg, False)
    assert outcome.ok
    assert outcome.config is not None
    assert outcome.config.allow_live_trading is False
    assert [(c.name, c.old, c.new) for c in outcome.changes] == [
        ("allow_live_trading", True, False)
    ]


def test_asking_for_the_state_it_already_has_is_not_a_change() -> None:
    lifted = make(allow_live_trading=True)
    again = set_live_override(lifted, True)
    assert again.ok and again.changes == (), "already on: nothing to confirm, nothing to record"
    already_off = set_live_override(make(), False)
    assert already_off.ok and already_off.changes == ()


def test_the_change_reads_as_one_line_for_the_log() -> None:
    outcome = set_live_override(make(), True, typed="Slave")
    assert describe_changes(outcome.changes) == "allow_live_trading off -> on"


def test_lifting_the_guard_is_flagged() -> None:
    outcome = set_live_override(make(), True, typed="Slave")
    (warning,) = advisories(outcome)
    assert "Slave" in warning and "prop" in warning


def test_putting_the_guard_back_needs_no_warning_of_its_own() -> None:
    """What it does to copies already open is asked about beforehand, like any other edit."""
    outcome = set_live_override(make(allow_live_trading=True), False)
    assert advisories(outcome) == []


def test_an_unrelated_edit_says_nothing_about_the_guard() -> None:
    outcome = apply_edits(make(allow_live_trading=True), {"risk_percent": "0.5"})
    assert advisories(outcome) == []


# --- rename: a name on the account, never gated by the orphan-confirm pipeline --------------


def _cfg_with_account(**overrides: object) -> AppConfig:
    return AppConfig(terminals={TID: make(last_login=123, last_server="A-Live", **overrides)})  # type: ignore[arg-type]


def test_rename_sets_the_accounts_name() -> None:
    cfg = _cfg_with_account()
    assert rename(cfg, TID, "My Funded Account") is None
    assert cfg.account_names[account_key(123, "A-Live")] == "My Funded Account"
    assert cfg.display_name(TID) == "My Funded Account"


def test_rename_strips_surrounding_whitespace() -> None:
    cfg = _cfg_with_account()
    rename(cfg, TID, "  My Funded Account  ")
    assert cfg.account_names[account_key(123, "A-Live")] == "My Funded Account"


def test_rename_to_blank_clears_it_back_to_the_label() -> None:
    cfg = _cfg_with_account()
    rename(cfg, TID, "My Funded Account")
    rename(cfg, TID, "")
    assert account_key(123, "A-Live") not in cfg.account_names
    assert cfg.display_name(TID) == "Slave"  # make()'s label, not the id fallback


def test_rename_refuses_names_outside_the_rule() -> None:
    """The profile-name rule plus spaces: refused before anything is written."""
    cfg = _cfg_with_account()
    for bad in ("x" * 33, "FTMO/100k", "été", "100k!", "-starts-with-dash", "a\tb"):
        refusal = rename(cfg, TID, bad)
        assert refusal is not None, bad
        assert "letters, digits, spaces" in refusal
    assert cfg.account_names == {}


def test_rename_allows_everything_the_rule_names() -> None:
    cfg = _cfg_with_account()
    for good in ("FTMO 100k", "E8-eval_2", "x" * 32, "9 lives"):
        assert rename(cfg, TID, good) is None, good


def test_rename_refuses_when_no_account_is_known_yet() -> None:
    cfg = AppConfig(terminals={TID: make()})  # no last_login
    refusal = rename(cfg, TID, "My Funded Account")
    assert refusal is not None
    assert "no account is known" in refusal
    assert cfg.account_names == {}


def test_rename_never_touches_any_other_field() -> None:
    """A name is cosmetic -- nothing about renaming should go anywhere near risk settings."""
    cfg = _cfg_with_account(risk_percent=2.5)
    rename(cfg, TID, "New Name")
    assert cfg.terminals[TID].risk_percent == 2.5
    assert cfg.terminals[TID].role is Role.SLAVE


def test_rename_follows_the_account_to_a_different_terminal() -> None:
    """The whole point of keying by account instead of terminal_id."""
    other_tid = "B" * 32
    cfg = AppConfig(
        terminals={
            TID: make(last_login=123, last_server="A-Live"),
            other_tid: TerminalConfig(
                terminal_id=other_tid, label="Other", last_login=123, last_server="A-Live"
            ),
        }
    )
    rename(cfg, TID, "My Funded Account")
    assert cfg.display_name(other_tid) == "My Funded Account"
