"""Wiring tests for the entry point: profile resolution and the single-instance lock.

Anything past ``main()`` launches the real desktop window or imports the MetaTrader5
extension, neither of which belongs in this suite; ``--check`` is exercised manually (see
README) and the window by the CDP harness.
"""

from __future__ import annotations

import pytest

from mt5copier.__main__ import main
from mt5copier.paths import lock_path
from mt5copier.single_instance import SingleInstanceLock


def test_an_unknown_profile_on_the_flag_is_refused_not_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo in a shortcut must not silently make an empty profile and run from it."""
    called = False

    def fake_run() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("mt5copier.desktop.run", fake_run)

    assert main(["--profile", "no-such"]) == 2
    assert not called
    out = capsys.readouterr().out
    assert "no profile called" in out
    from mt5copier.profiles import profile_dir

    assert not profile_dir("no-such").exists(), "the refusal must not create the directory"


def test_a_bare_launch_opens_the_last_opened_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window manages profiles itself (the titlebar pill), so a bare launch opens
    whichever profile ran last, with no picker in between."""
    from mt5copier import profiles
    from mt5copier.paths import active_profile, profiles_root, user_data_dir

    profiles.create("funded")
    # last_opened is the lock file's mtime; give "funded" one so it beats the default,
    # which has never run in this isolated data dir.
    (profiles.profile_dir("funded") / "mt5copier.lock").write_text("\n")

    seen: dict[str, object] = {}

    def fake_run() -> None:
        seen["profile"] = active_profile()
        seen["dir"] = user_data_dir()

    monkeypatch.setattr("mt5copier.desktop.run", fake_run)

    assert main([]) == 0
    assert seen["profile"] == "funded"
    assert seen["dir"] == profiles_root() / "funded"


def test_a_bare_launch_opens_the_default_when_no_profile_ever_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mt5copier.paths import active_profile

    seen: dict[str, object] = {}

    def fake_run() -> None:
        seen["profile"] = active_profile()

    monkeypatch.setattr("mt5copier.desktop.run", fake_run)

    assert main([]) == 0
    assert seen["profile"] == "default"


def test_the_old_desktop_flag_is_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shortcuts and the profiles pill pass --desktop; it is the default and only mode now,
    so the flag must stay a harmless no-op."""
    from mt5copier.paths import active_profile

    seen: dict[str, object] = {}

    def fake_run() -> None:
        seen["profile"] = active_profile()

    monkeypatch.setattr("mt5copier.desktop.run", fake_run)

    assert main(["--desktop"]) == 0
    assert seen["profile"] == "default"


def test_a_second_launch_focuses_the_running_window(
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

        assert main(["--profile", "default"]) == 0
        assert focused == [os.getpid()], "it must focus the lock holder's window"
        assert "already running" not in capsys.readouterr().out
    finally:
        held.release()


def test_a_failed_focus_still_says_already_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No window to focus (the holder is wedged before its window exists, say): fall back
    to the words, and never start a second window."""
    held = SingleInstanceLock(lock_path())
    held.acquire()
    try:
        monkeypatch.setattr("mt5copier.winfocus.focus_window_of_pid", lambda pid: False)
        monkeypatch.setattr(
            "mt5copier.desktop.run",
            lambda: (_ for _ in ()).throw(AssertionError("the window must not start")),
        )

        assert main(["--profile", "default"]) == 1
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
