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


def test_bare_desktop_skips_the_picker_and_opens_the_last_opened_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window manages profiles itself (the titlebar pill), so --desktop without
    --profile must never route through the terminal picker."""
    from mt5copier import profiles
    from mt5copier.paths import active_profile

    profiles.create("funded")
    # last_opened is the lock file's mtime; give "funded" one so it beats the default,
    # which has never run in this isolated data dir.
    (profiles.profile_dir("funded") / "mt5copier.lock").write_text("\n")

    def no_picker() -> None:
        raise AssertionError("--desktop must not open the terminal picker")

    seen: dict[str, object] = {}

    def fake_run() -> None:
        seen["profile"] = active_profile()

    monkeypatch.setattr("mt5copier.tui.picker.pick_profile", no_picker)
    monkeypatch.setattr("mt5copier.desktop.run", fake_run)

    assert main(["--desktop"]) == 0
    assert seen["profile"] == "funded"


def test_bare_desktop_opens_the_default_when_no_profile_ever_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mt5copier.paths import active_profile

    seen: dict[str, object] = {}

    def fake_run() -> None:
        seen["profile"] = active_profile()

    monkeypatch.setattr(
        "mt5copier.tui.picker.pick_profile",
        lambda: (_ for _ in ()).throw(AssertionError("picker opened")),
    )
    monkeypatch.setattr("mt5copier.desktop.run", fake_run)

    assert main(["--desktop"]) == 0
    assert seen["profile"] == "default"


def test_a_second_desktop_launch_focuses_the_running_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A double-click relaunch of an open profile must focus its window and exit clean --
    the frozen exe has no console for the 'already running' line to land in."""
    import os

    held = SingleInstanceLock(lock_path())
    held.acquire()
    try:
        focused: list[int] = []

        def fake_focus(pid: int) -> bool:
            focused.append(pid)
            return True

        monkeypatch.setattr("mt5copier.winfocus.focus_window_of_pid", fake_focus)
        monkeypatch.setattr(
            "mt5copier.desktop.run",
            lambda: (_ for _ in ()).throw(AssertionError("the window must not start")),
        )

        assert main(["--desktop", "--profile", "default"]) == 0
        assert focused == [os.getpid()], "it must focus the lock holder's window"
        assert "already running" not in capsys.readouterr().out
    finally:
        held.release()


def test_a_failed_focus_still_says_already_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No window to focus (a TUI run holds the profile, say): fall back to the words."""
    held = SingleInstanceLock(lock_path())
    held.acquire()
    try:
        monkeypatch.setattr("mt5copier.winfocus.focus_window_of_pid", lambda pid: False)

        assert main(["--desktop", "--profile", "default"]) == 1
        assert "already running" in capsys.readouterr().out
    finally:
        held.release()


def test_diagnostic_flags_never_touch_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """--paths is read-only and must work even while a real instance holds the lock."""
    held = SingleInstanceLock(lock_path())
    held.acquire()
    try:
        assert main(["--paths"]) == 0
    finally:
        held.release()
