"""The profile picker: list, open, create, rename, delete -- driven through the real app.

Against the suite's isolated %APPDATA%, like everything else; the picker touches no lock
and starts no engine, so these stay cheap.
"""

from __future__ import annotations

from textual.widgets import DataTable, Static

from mt5copier import profiles
from mt5copier.paths import DEFAULT_PROFILE
from mt5copier.single_instance import SingleInstanceLock
from mt5copier.tui.picker import PickerApp

SIZE = (120, 30)


async def test_a_fresh_install_shows_one_row_and_enter_opens_it() -> None:
    app = PickerApp()
    async with app.run_test(size=SIZE) as pilot:
        table = app.query_one("#table", DataTable)
        assert table.row_count == 1
        await pilot.press("enter")
    assert app.return_value == DEFAULT_PROFILE


async def test_making_a_profile_and_opening_it() -> None:
    app = PickerApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("n")
        await pilot.press(*"funded")
        await pilot.press("enter")  # creates and selects it
        assert profiles.exists("funded")
        await pilot.press("enter")  # opens the selected row
    assert app.return_value == "funded"


async def test_a_bad_name_is_refused_in_place() -> None:
    app = PickerApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("n")
        await pilot.press(*"-bad", "enter")
        status = app.query_one("#status", Static)
        assert "letters, digits" in str(status.content)
        assert not profiles.exists("-bad")


async def test_delete_asks_first_and_y_confirms() -> None:
    profiles.create("doomed")
    app = PickerApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("down")  # onto 'doomed', below the default
        await pilot.press("d")
        assert profiles.exists("doomed"), "d alone deletes nothing"
        await pilot.press("y")
        assert not profiles.exists("doomed")
        assert app.query_one("#table", DataTable).row_count == 1


async def test_escape_backs_out_of_a_delete() -> None:
    profiles.create("kept")
    app = PickerApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("down", "d", "escape", "y")
        assert profiles.exists("kept"), "y after escape must do nothing"


async def test_rename_moves_the_directory() -> None:
    profiles.create("old-name")
    app = PickerApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("down", "e")
        from textual.widgets import Input

        field = app.query_one("#name", Input)
        assert field.value == "old-name", "prefilled with the current name"
        field.value = ""  # a person would edit it; the keystrokes for that are Input's own
        await pilot.press(*"new-name", "enter")
        assert profiles.exists("new-name") and not profiles.exists("old-name")


async def test_a_running_profile_is_shown_running_and_refuses_to_open() -> None:
    path = profiles.create("busy")
    lock = SingleInstanceLock(path / "mt5copier.lock")
    lock.acquire()
    try:
        app = PickerApp()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("down", "enter")
            status = app.query_one("#status", Static)
            assert "already running" in str(status.content)
            assert app.return_value is None
    finally:
        lock.release()


async def test_q_quits_with_nothing() -> None:
    app = PickerApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("q")
    assert app.return_value is None


async def test_q_while_typing_a_name_is_just_a_letter() -> None:
    app = PickerApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("n")
        await pilot.press(*"sq")
        assert app.return_value is None, "the app must not quit mid-typing"
        await pilot.press("enter")
        assert profiles.exists("sq")
