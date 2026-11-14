"""Wiring tests for the entry point -- just the single-instance lock's effect on ``main()``.

Everything else here launches a real Textual app or imports the MetaTrader5 extension, neither
of which belongs in this suite; those are covered by ``--check`` being exercised manually
(see README) and by ``tests/test_tui.py`` driving the app directly.
"""

from __future__ import annotations

import pytest

from mt5copier.__main__ import main
from mt5copier.paths import lock_path
from mt5copier.single_instance import SingleInstanceLock


def test_a_second_full_run_is_refused_while_one_is_already_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    held = SingleInstanceLock(lock_path())
    held.acquire()
    try:
        called = False

        def fake_run() -> None:
            nonlocal called
            called = True

        monkeypatch.setattr("mt5copier.tui.run", fake_run)

        assert main(["--profile", "default"]) == 1
        assert not called, "the TUI must never start once the lock is refused"
        assert "already running" in capsys.readouterr().out
    finally:
        held.release()


def test_an_unknown_profile_on_the_flag_is_refused_not_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo in a shortcut must not silently make an empty profile and run from it."""
    called = False

    def fake_run() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("mt5copier.tui.run", fake_run)

    assert main(["--profile", "no-such"]) == 2
    assert not called
    out = capsys.readouterr().out
    assert "no profile called" in out
    from mt5copier.profiles import profile_dir

    assert not profile_dir("no-such").exists(), "the refusal must not create the directory"


def test_quitting_the_picker_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_run() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("mt5copier.tui.run", fake_run)
    monkeypatch.setattr("mt5copier.tui.picker.pick_profile", lambda: None)

    assert main([]) == 0
    assert not called


def test_the_pickers_choice_decides_the_data_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mt5copier import profiles
    from mt5copier.paths import active_profile, profiles_root, user_data_dir

    profiles.create("funded")
    seen: dict[str, object] = {}

    def fake_run() -> None:
        seen["profile"] = active_profile()
        seen["dir"] = user_data_dir()

    monkeypatch.setattr("mt5copier.tui.run", fake_run)
    monkeypatch.setattr("mt5copier.tui.picker.pick_profile", lambda: "funded")

    assert main([]) == 0
    assert seen["profile"] == "funded"
    assert seen["dir"] == profiles_root() / "funded"


def test_diagnostic_flags_never_touch_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """--paths is read-only and must work even while a real instance holds the lock."""
    held = SingleInstanceLock(lock_path())
    held.acquire()
    try:
        assert main(["--paths"]) == 0
    finally:
        held.release()
