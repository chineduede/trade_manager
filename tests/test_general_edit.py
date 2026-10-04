"""The copier's own settings: checked against the model, and the mode guarded twice."""

from __future__ import annotations

import pytest

from mt5copier.config import AppConfig
from mt5copier.general_edit import (
    DRY_RUN,
    apply_general,
    commit_general,
    initial_general,
)


def test_unchanged_values_change_nothing() -> None:
    cfg = AppConfig()
    outcome = apply_general(cfg, initial_general(cfg))
    assert outcome.ok and outcome.changes == ()


def test_timing_is_parsed_checked_and_committed() -> None:
    cfg = AppConfig()
    outcome = apply_general(
        cfg,
        {
            "poll_interval_ms": "500",
            "heartbeat_timeout_s": "20",
            "reconnect_backoff_s": "1, 3; 10 60",
            "join_grace_seconds": "2.5",
        },
    )
    assert outcome.ok, outcome.errors
    commit_general(cfg, outcome)
    assert cfg.poll_interval_ms == 500
    assert cfg.heartbeat_timeout_s == 20.0
    assert cfg.reconnect_backoff_s == [1.0, 3.0, 10.0, 60.0]
    assert cfg.join_grace_seconds == 2.5


@pytest.mark.parametrize(
    ("name", "text", "said"),
    [
        ("poll_interval_ms", "20", "must be at least 50"),
        ("poll_interval_ms", "1.5", "must be a whole number"),
        ("join_grace_seconds", "6", "must be at most 5"),
        ("heartbeat_timeout_s", "0", "must be greater than 0"),
        ("heartbeat_timeout_s", "", "required"),
        ("reconnect_backoff_s", "1, 0", "every wait must be more than 0"),
        ("reconnect_backoff_s", " ", "give at least one wait"),
        ("join_grace_seconds", "nan", "must be a finite number"),
    ],
)
def test_bad_values_are_named_and_nothing_is_kept(name: str, text: str, said: str) -> None:
    cfg = AppConfig()
    outcome = apply_general(cfg, {name: text})
    assert not outcome.ok
    assert outcome.errors[name] == said
    with pytest.raises(ValueError):
        commit_general(cfg, outcome)


def test_going_live_asks_for_the_word_then_takes_it() -> None:
    cfg = AppConfig()
    asked = apply_general(cfg, {DRY_RUN: False})
    assert asked.confirm_live and not asked.ok and not asked.errors

    wrong = apply_general(cfg, {DRY_RUN: False}, typed="yes")
    assert not wrong.ok and "LIVE" in wrong.errors[DRY_RUN]

    right = apply_general(cfg, {DRY_RUN: False}, typed=" live ")
    assert right.ok
    commit_general(cfg, right)
    assert cfg.dry_run is False


def test_going_back_to_a_dry_run_needs_no_word() -> None:
    cfg = AppConfig(dry_run=False)
    outcome = apply_general(cfg, {DRY_RUN: True})
    assert outcome.ok and not outcome.confirm_live


@pytest.mark.parametrize("dry_run", [True, False])
def test_the_mode_cannot_change_while_copying_runs(dry_run: bool) -> None:
    cfg = AppConfig(dry_run=dry_run, copying_enabled=True)
    outcome = apply_general(cfg, {DRY_RUN: not dry_run}, typed="LIVE")
    assert not outcome.ok
    assert "stop it with d" in outcome.errors[DRY_RUN]


def test_timing_can_change_while_copying_runs() -> None:
    cfg = AppConfig(copying_enabled=True)
    assert apply_general(cfg, {"poll_interval_ms": "300"}).ok


def test_a_field_that_is_not_here_is_a_bug() -> None:
    with pytest.raises(ValueError):
        apply_general(AppConfig(), {"copying_enabled": True})


def test_closing_terminals_is_a_choice_of_three_and_can_change_while_copying() -> None:
    from mt5copier.config import TerminalClosing

    cfg = AppConfig(copying_enabled=True)
    assert cfg.close_terminals is TerminalClosing.LAUNCHED, "today's behaviour by default"
    outcome = apply_general(cfg, {"close_terminals": "all"})
    assert outcome.ok
    commit_general(cfg, outcome)
    assert cfg.close_terminals is TerminalClosing.ALL
    assert not apply_general(cfg, {"close_terminals": "sometimes"}).ok
