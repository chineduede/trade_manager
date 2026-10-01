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

        assert main([]) == 1
        assert not called, "the TUI must never start once the lock is refused"
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
