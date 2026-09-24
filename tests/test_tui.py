"""UI tests via Textual's headless harness.

The Engine is stubbed out, so these tests spawn no processes and touch no MT5 terminal. What
they verify is that key presses and clicks produce the right *config* changes and that the
changes are persisted -- the role assignment rules are the part where a mistake would put real
trades on the wrong account.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Checkbox, Input, Select, Static, TextArea

from mt5copier.config import AppConfig, NoStopPolicy, Role, SizingMode, TerminalConfig
from mt5copier.engine import RecordedAction, RecordedResult, TerminalView
from mt5copier.settings_edit import invert_refusal
from mt5copier.store import Orphan
from mt5copier.tui.app import CopierApp
from mt5copier.tui.settings_screen import SettingsScreen

TID_MASTER = "A" * 32
TID_SLAVE = "B" * 32
TID_BROKEN = "C" * 32


class StubEngine:
    """Engine stand-in: records calls, spawns nothing."""

    def __init__(self, cfg: AppConfig, state_path: Path | None = None) -> None:
        self._cfg = cfg
        self.sync_calls = 0
        self.restarted: list[str] = []
        self.reconnected: list[str] = []
        self.started = False
        self.stopped = False
        self.cleared = 0
        self.held: dict[str, str] = {}
        self.found_orphans: dict[str, list[Orphan]] = {}
        self.acknowledged: list[str] = []
        self.results: list[RecordedResult] = []
        self.open_copies: dict[str, int | None] = {}
        """What the stub says each slave has open. ``None`` is a count that could not be read."""

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def sync(self) -> None:
        self.sync_calls += 1

    def restart(self, terminal_id: str) -> None:
        self.restarted.append(terminal_id)

    def reconnect(self, terminal_id: str) -> None:
        self.reconnected.append(terminal_id)

    def views(self) -> list[TerminalView]:
        views = [TerminalView(cfg=t.model_copy(deep=True)) for t in self._cfg.terminals.values()]
        views.sort(key=lambda v: (v.cfg.label.lower(), v.cfg.terminal_id))
        return views

    def health(self) -> dict[str, int]:
        return {"total": len(self._cfg.terminals), "supervised": 0, "live": 0, "stale": 0}

    # --- Stage 2a detection surface ---------------------------------------------------------

    def recent_actions(self, limit: int | None = None) -> list[RecordedAction]:
        return []

    @property
    def action_total(self) -> int:
        return 0

    @property
    def blocked_reason(self) -> str | None:
        return None

    def believed_copies(self) -> int:
        return 0

    def clear_detection_state(self) -> None:
        self.cleared += 1

    # --- Stage 2c startup reconciliation ----------------------------------------------------

    def holds(self) -> dict[str, str]:
        return dict(self.held)

    def orphans(self, terminal_id: str | None = None) -> list[Orphan]:
        if terminal_id is not None:
            return list(self.found_orphans.get(terminal_id, []))
        return [o for items in self.found_orphans.values() for o in items]

    def acknowledge_orphans(self, terminal_id: str) -> int:
        self.acknowledged.append(terminal_id)
        found = self.found_orphans.get(terminal_id, [])
        self.held.pop(terminal_id, None)
        return len(found)

    # --- Stage 2c dispatch ------------------------------------------------------------------

    def recent_results(self, limit: int | None = None) -> list[RecordedResult]:
        return list(self.results) if limit is None else list(self.results)[-limit:]

    @property
    def failed_total(self) -> int:
        return sum(1 for r in self.results if not r.result.ok)

    # --- the settings editor ----------------------------------------------------------------

    def copies_open_on(self, terminal_id: str) -> int | None:
        return self.open_copies.get(terminal_id, 0)

    def apply_settings(self, new: TerminalConfig) -> str | None:
        """The real engine's contract: refuse a flip of ``invert`` under copies, else swap."""
        old = self._cfg.terminals[new.terminal_id]
        if new.invert != old.invert:
            count = self.copies_open_on(new.terminal_id)
            if count is None or count > 0:
                return f"invert: {invert_refusal(count)}"
        self._cfg.replace_terminal(new)
        return None


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        terminals={
            TID_MASTER: TerminalConfig(
                terminal_id=TID_MASTER, label="Alpha Broker", exe_path=exe, last_login=111
            ),
            TID_SLAVE: TerminalConfig(
                terminal_id=TID_SLAVE, label="Beta Broker", exe_path=exe, last_login=222
            ),
            # No exe_path: an uninstalled leftover, like the EXNESS install on this machine.
            TID_BROKEN: TerminalConfig(
                terminal_id=TID_BROKEN, label="Gone Broker", exe_path=None, last_login=333
            ),
        }
    )


@pytest.fixture
def app(cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CopierApp:
    """App wired to the stub engine, saving to a temp config file."""
    monkeypatch.setattr("mt5copier.tui.app.Engine", StubEngine)
    saved = tmp_path / "config.json"
    monkeypatch.setattr("mt5copier.config.config_path", lambda: saved)
    monkeypatch.setattr("mt5copier.tui.app.config_path", lambda: saved)
    instance = CopierApp(cfg)
    instance._saved_to = saved  # type: ignore[attr-defined]
    return instance


def row_of(app: CopierApp, terminal_id: str) -> int:
    return app._row_ids.index(terminal_id)


# --- rendering -----------------------------------------------------------------------------


async def test_table_lists_every_terminal_sorted(app: CopierApp) -> None:
    async with app.run_test():
        assert app._row_ids == [TID_MASTER, TID_SLAVE, TID_BROKEN]


async def test_unassigned_terminals_are_not_connected(app: CopierApp, cfg: AppConfig) -> None:
    """Nothing is launched until a role is assigned -- that is the whole point of Stage 1."""
    async with app.run_test():
        assert cfg.assigned() == []


async def test_statusbar_reports_no_master_initially(app: CopierApp) -> None:
    async with app.run_test():
        from textual.widgets import Static

        text = str(app.query_one("#statusbar", Static).render())
        assert "master: (none)" in text
        assert "detection off" in text


async def test_a_dry_run_and_a_live_session_do_not_look_alike(
    app: CopierApp, cfg: AppConfig
) -> None:
    """The single most important thing this UI has to communicate. A copier that looks the same
    whether or not it can send orders is one nobody can trust.
    """
    async with app.run_test():
        from textual.widgets import Static

        cfg.copying_enabled = True
        cfg.dry_run = True
        app._refresh()
        dry = str(app.query_one("#statusbar", Static).render())

        cfg.dry_run = False
        app._refresh()
        live = str(app.query_one("#statusbar", Static).render())

        assert "nothing sent" in dry
        assert "COPYING LIVE" in live
        assert "LIVE" in app.sub_title


# --- role assignment -----------------------------------------------------------------------


async def test_pressing_m_sets_master(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await pilot.press("m")
        assert cfg.master is not None
        assert cfg.master.terminal_id == TID_MASTER


async def test_master_is_exclusive(app: CopierApp, cfg: AppConfig) -> None:
    """Assigning a second master must demote the first, not fail validation later."""
    async with app.run_test() as pilot:
        await pilot.press("m")
        await pilot.press("down")
        await pilot.press("m")
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED
        assert cfg.terminals[TID_SLAVE].role is Role.MASTER


async def test_pressing_s_toggles_slave(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await pilot.press("s")
        assert cfg.terminals[TID_MASTER].role is Role.SLAVE
        await pilot.press("s")
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED


async def test_cannot_make_the_master_a_slave_by_accident(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await pilot.press("m")
        await pilot.press("s")
        assert cfg.terminals[TID_MASTER].role is Role.MASTER


async def test_terminal_without_exe_cannot_be_assigned(app: CopierApp, cfg: AppConfig) -> None:
    """An uninstalled terminal is visible but must never be designated."""
    async with app.run_test() as pilot:
        app.query_one("#terminals").move_cursor(row=row_of(app, TID_BROKEN))
        await pilot.press("m")
        await pilot.press("s")
        assert cfg.terminals[TID_BROKEN].role is Role.UNASSIGNED


async def test_unassign_clears_role(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await pilot.press("m")
        await pilot.press("u")
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED


async def test_role_change_is_persisted(app: CopierApp) -> None:
    from mt5copier import config as config_mod

    async with app.run_test() as pilot:
        await pilot.press("m")
        reloaded = config_mod.load(app._saved_to)  # type: ignore[attr-defined]
        assert reloaded.master is not None
        assert reloaded.master.terminal_id == TID_MASTER


async def test_role_change_triggers_engine_sync(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        before = app._engine.sync_calls  # type: ignore[attr-defined]
        await pilot.press("m")
        assert app._engine.sync_calls > before  # type: ignore[attr-defined]


# --- mouse ---------------------------------------------------------------------------------


async def _select_cell(app: CopierApp, pilot: object, terminal_id: str, column: int) -> None:
    """Emit the CellSelected message the DataTable would send for a click on that cell."""
    from textual.coordinate import Coordinate
    from textual.widgets import DataTable

    table = app.query_one("#terminals", DataTable)
    coord = Coordinate(row_of(app, terminal_id), column)
    table.move_cursor(row=coord.row, column=coord.column)
    table.post_message(
        DataTable.CellSelected(
            table,
            value=table.get_cell_at(coord),
            coordinate=coord,
            cell_key=table.coordinate_to_cell_key(coord),
        )
    )
    await pilot.pause()  # type: ignore[attr-defined]


async def test_clicking_master_column_assigns_master(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await _select_cell(app, pilot, TID_SLAVE, column=0)
        assert cfg.terminals[TID_SLAVE].role is Role.MASTER


async def test_clicking_slave_column_toggles_slave(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await _select_cell(app, pilot, TID_SLAVE, column=1)
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE


async def test_clicking_other_columns_does_not_change_roles(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await _select_cell(app, pilot, TID_SLAVE, column=2)  # the BROKER column
        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED


# --- panic ---------------------------------------------------------------------------------


# --- detection (stage 2a) -------------------------------------------------------------------


async def test_detection_needs_a_master(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await pilot.press("d")
        assert cfg.copying_enabled is False


async def test_detection_needs_at_least_one_slave(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await pilot.press("m")
        await pilot.press("d")
        assert cfg.copying_enabled is False


async def test_pressing_d_toggles_detection(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await pilot.press("m")
        app.query_one("#terminals").move_cursor(row=row_of(app, TID_SLAVE))
        await pilot.press("s")

        await pilot.press("d")
        assert cfg.copying_enabled is True
        assert cfg.dry_run is True, "turning detection on must never turn dry run off"

        await pilot.press("d")
        assert cfg.copying_enabled is False


async def test_detection_state_can_be_cleared(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        await pilot.press("ctrl+l")
        assert app._engine.cleared == 1  # type: ignore[attr-defined]


async def test_o_acknowledges_orphans_on_the_selected_slave(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        engine = app._engine  # type: ignore[attr-defined]
        tid = app._row_ids[0]
        engine.found_orphans[tid] = [Orphan(tid, 5521, "EURUSD", 0.12)]
        engine.held[tid] = "1 untracked position(s) of ours: #5521"
        await pilot.press("o")

        assert engine.acknowledged == [tid]


async def test_a_held_slave_is_announced_in_the_action_panel(app: CopierApp) -> None:
    """A hold is one slave going quiet while the others carry on, which is the easiest kind of
    silence to miss."""
    async with app.run_test() as pilot:
        engine = app._engine  # type: ignore[attr-defined]
        tid = app._row_ids[0]
        engine.held[tid] = "1 untracked position(s) of ours: #5521"
        await pilot.pause()
        app._refresh()

        from textual.widgets import Static

        rendered = str(app.query_one("#actions", Static).render())
        assert "HELD" in rendered


async def test_panic_halts_copying_and_leaves_roles_alone(app: CopierApp, cfg: AppConfig) -> None:
    """Panic must halt copying without disturbing positions or role assignment.

    It clears the flag and nothing else -- no position is closed or modified, at any stage.
    """
    async with app.run_test() as pilot:
        await pilot.press("m")
        cfg.copying_enabled = True
        await pilot.press("f9")
        assert cfg.copying_enabled is False
        assert cfg.terminals[TID_MASTER].role is Role.MASTER


# --- worker controls -----------------------------------------------------------------------


async def test_reconnect_targets_selected_terminal(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        await pilot.press("c")
        assert app._engine.reconnected == [TID_MASTER]  # type: ignore[attr-defined]


async def test_restart_targets_selected_terminal(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        assert app._engine.restarted == [TID_MASTER]  # type: ignore[attr-defined]


async def test_engine_stopped_on_exit(app: CopierApp) -> None:
    async with app.run_test():
        pass
    assert app._engine.stopped is True  # type: ignore[attr-defined]


async def test_a_refresh_tick_during_teardown_is_harmless(app: CopierApp) -> None:
    """Textual removes the widgets before it stops the timers, so a tick can land with nothing to
    draw into. That used to surface as an intermittent NoMatches out of the exit of a test, and
    would have been a traceback on quitting the real app."""
    from textual.css.query import NoMatches

    async with app.run_test():
        await app.screen_stack[0].remove_children()
        with pytest.raises(NoMatches):
            app._refresh()  # a direct call still fails loudly, so a bad widget id cannot hide
        app._on_timer()  # the timer's own path shrugs it off


# --- the settings editor ---------------------------------------------------------------------
#
# What is worth testing here is not that a text box takes text but that the form is safe to have
# open while copying is live: it must not leak keystrokes into role assignment, it must not
# disable the panic key, a refused entry must change nothing, and an accepted one must reach the
# config, the disk and the log.


async def _open_editor(
    app: CopierApp, pilot: object, terminal_id: str = TID_SLAVE
) -> SettingsScreen:
    """Make ``terminal_id`` a slave, select it, and open its settings."""
    app.query_one("#terminals").move_cursor(row=row_of(app, terminal_id))
    await pilot.press("s")  # type: ignore[attr-defined]
    await pilot.press("e")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    return screen


def _toasts(app: CopierApp) -> list[str]:
    return [n.message for n in app._notifications]


async def test_e_opens_the_editor_for_the_selected_terminal(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        title = str(screen.query_one("#title", Static).render())
        assert "Beta Broker" in title
        assert "slave" in title


async def test_the_form_starts_at_the_terminals_current_values(
    app: CopierApp, cfg: AppConfig
) -> None:
    cfg.terminals[TID_SLAVE].risk_percent = 0.35
    cfg.terminals[TID_SLAVE].max_lot = 4.0
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        assert screen.query_one("#f-risk_percent", Input).value == "0.35"
        assert screen.query_one("#f-max_lot", Input).value == "4"
        assert screen.query_one("#f-sizing_mode", Select).value == "percent_of_balance"
        assert screen.query_one("#f-copy_sltp", Checkbox).value is True
        assert screen.query_one("#f-copy_pending", Checkbox).value is False


async def test_saving_an_edit_reaches_the_config_the_disk_and_the_engine(
    app: CopierApp, cfg: AppConfig
) -> None:
    from mt5copier import config as config_mod

    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        before = cfg.terminals[TID_SLAVE]
        syncs = app._engine.sync_calls  # type: ignore[attr-defined]

        screen.query_one("#f-risk_percent", Input).value = "0.4"
        screen.query_one("#f-copy_pending", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()

        live = cfg.terminals[TID_SLAVE]
        assert live.risk_percent == 0.4
        assert live.copy_pending is True
        assert live is not before, "an edit is swapped in whole, never patched field by field"
        assert live.role is Role.SLAVE, "editing settings must not touch the role"
        assert not isinstance(app.screen, SettingsScreen), "a saved form closes"
        assert app._engine.sync_calls > syncs  # type: ignore[attr-defined]

        reloaded = config_mod.load(app._saved_to)  # type: ignore[attr-defined]
        assert reloaded.terminals[TID_SLAVE].risk_percent == 0.4
        assert reloaded.terminals[TID_SLAVE].copy_pending is True


async def test_the_save_button_does_the_same_as_the_key(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(120, 45)) as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-max_deviation_points", Input).value = "55"
        await pilot.click("#save")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].max_deviation_points == 55


async def test_enter_in_a_text_box_saves(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        box = screen.query_one("#f-risk_cash", Input)
        box.value = "250"
        box.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].risk_cash == 250


async def test_a_choice_is_saved(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-sizing_mode", Select).value = "fixed_cash_risk"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].sizing_mode is SizingMode.FIXED_CASH_RISK


async def test_the_risk_column_follows_an_edit(app: CopierApp) -> None:
    """The table displays RISK and used to be unable to change it; it should now follow."""
    from textual.widgets import DataTable

    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-risk_percent", Input).value = "2.5"
        await pilot.press("ctrl+s")
        await pilot.pause()
        table = app.query_one("#terminals", DataTable)
        assert "2.5%" in str(table.get_cell(TID_SLAVE, "risk"))


# --- a refused entry changes nothing -------------------------------------------------------


async def test_a_bad_entry_keeps_the_form_open_and_says_why(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        # Opening the editor made the terminal a slave, which saved; what matters is that the
        # refused edit adds nothing to that.
        on_disk = app._saved_to.read_bytes()  # type: ignore[attr-defined]
        screen.query_one("#f-risk_percent", Input).value = "0"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen), "a refused form stays open to be fixed"
        assert "greater than" in str(screen.query_one("#e-risk_percent", Static).render())
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0
        assert app._saved_to.read_bytes() == on_disk, "nothing was written"  # type: ignore[attr-defined]


async def test_one_bad_entry_blocks_the_good_ones_beside_it(app: CopierApp, cfg: AppConfig) -> None:
    """Half an edit is the worst outcome: a form that saved what it liked and dropped the rest
    would leave the user believing the whole thing went in."""
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-copy_pending", Checkbox).value = True
        screen.query_one("#f-risk_percent", Input).value = "abc"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].copy_pending is False


async def test_an_error_message_is_not_clipped_on_an_80_column_terminal(app: CopierApp) -> None:
    """The number in "must be at most 100" is the part that says what to type instead, and it
    is the part a fixed-height row cuts off. Read what was actually drawn, not what was set."""
    import html

    async with app.run_test(size=(80, 44)) as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-risk_percent", Input).value = "150"
        screen.query_one("#f-risk_cash", Input).value = "0,5"
        await pilot.press("ctrl+s")
        await pilot.pause(0.3)
        drawn = html.unescape(app.export_screenshot()).replace("\xa0", " ")
        assert "must be at most 100" in drawn
        assert "decimals" in drawn, "a long message wraps onto a second line instead of clipping"


async def test_every_bad_entry_is_flagged_at_once(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-risk_percent", Input).value = "abc"
        screen.query_one("#f-max_deviation_points", Input).value = "-1"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert str(screen.query_one("#e-risk_percent", Static).render()).strip()
        assert str(screen.query_one("#e-max_deviation_points", Static).render()).strip()


async def test_the_first_bad_field_takes_focus(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-max_lot", Input).value = "-3"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.focused is screen.query_one("#f-max_lot", Input)


async def test_fixing_a_bad_entry_lets_the_save_through(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        box = screen.query_one("#f-risk_percent", Input)
        box.value = "0"
        await pilot.press("ctrl+s")
        await pilot.pause()
        box.value = "0.6"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].risk_percent == 0.6
        assert not isinstance(app.screen, SettingsScreen)


async def test_escape_discards_the_form(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        on_disk = app._saved_to.read_bytes()  # type: ignore[attr-defined]
        screen.query_one("#f-risk_percent", Input).value = "9"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0
        assert app._saved_to.read_bytes() == on_disk  # type: ignore[attr-defined]


async def test_saving_without_changing_anything_writes_nothing(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        await _open_editor(app, pilot)
        before = cfg.terminals[TID_SLAVE]
        syncs = app._engine.sync_calls  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE] is before
        assert app._engine.sync_calls == syncs  # type: ignore[attr-defined]
        assert any("no changes" in t for t in _toasts(app))


# --- safe to have open while copying is live -----------------------------------------------


async def test_keys_typed_in_the_form_do_not_reach_role_assignment(
    app: CopierApp, cfg: AppConfig
) -> None:
    """m, s and u assign roles on the selected terminal, which is the very terminal whose form
    is open. With a checkbox focused there is no text box to absorb them."""
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-copy_pending", Checkbox).focus()
        await pilot.pause()
        roles = {t: c.role for t, c in cfg.terminals.items()}
        for key in ("m", "s", "u", "d", "c"):
            await pilot.press(key)
        assert {t: c.role for t, c in cfg.terminals.items()} == roles
        assert cfg.copying_enabled is False
        assert app._engine.reconnected == []  # type: ignore[attr-defined]
        assert isinstance(app.screen, SettingsScreen)


async def test_the_panic_key_still_works_with_the_form_open(app: CopierApp, cfg: AppConfig) -> None:
    """Copying can be live while the form is open, and a modal hides the app's own keys."""
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        cfg.copying_enabled = True
        screen.query_one("#f-risk_percent", Input).value = "3"
        await pilot.press("f9")
        await pilot.pause()
        assert cfg.copying_enabled is False
        assert not isinstance(app.screen, SettingsScreen), "the form closes after a panic"
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0, "and its unsaved edit is dropped"


async def test_tabbing_never_stops_on_the_dialog_frame(app: CopierApp) -> None:
    """A scroll container is focusable by default, which would put the frame in the tab order."""
    async with app.run_test() as pilot:
        await _open_editor(app, pilot)
        seen = set()
        for _ in range(20):
            await pilot.press("tab")
            seen.add(app.focused.id if app.focused else None)
        assert "dialog" not in seen
        assert {"save", "cancel", "f-risk_percent", "f-copy_pending"} <= seen


async def test_save_is_reachable_on_a_small_terminal(app: CopierApp) -> None:
    """At 80x24 the form is taller than the screen; the buttons must scroll into view rather than
    sit clipped off the bottom."""
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#save").focus()
        await pilot.pause(0.3)
        region = screen.query_one("#save").region
        assert region.y >= 0
        assert region.y + region.height <= 24


async def test_the_form_says_whether_it_is_live(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        cfg.dry_run = True
        screen = await _open_editor(app, pilot)
        assert "dry run" in str(screen.query_one("#mode", Static).render())
        await pilot.press("escape")
        await pilot.pause()

        cfg.dry_run = False
        await pilot.press("e")
        await pilot.pause()
        assert "LIVE" in str(app.screen.query_one("#mode", Static).render())


# --- what the form must not offer ----------------------------------------------------------


async def test_the_form_has_no_field_for_the_live_trading_override_the_name_or_the_role(
    app: CopierApp,
) -> None:
    """The override is the demo-only guard's; the name is discovery's, which would overwrite an
    edit on the next rescan; the role has its own keys."""
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        for name in ("allow_live_trading", "label", "role", "terminal_id", "exe_path"):
            assert not screen.query(f"#f-{name}"), name


async def test_the_form_says_what_it_leaves_out(app: CopierApp, cfg: AppConfig) -> None:
    cfg.terminals[TID_SLAVE].allow_live_trading = True
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        note = str(screen.query_one("#readonly", Static).render())
        assert "live trading" in note
        assert "(on)" in note
        assert "discovery" in note


# --- the symbol section ----------------------------------------------------------------------


async def test_the_symbol_fields_start_at_the_terminals_values(
    app: CopierApp, cfg: AppConfig
) -> None:
    slave = cfg.terminals[TID_SLAVE]
    slave.symbol_prefix = "m."
    slave.symbol_suffix = "p"
    slave.symbol_map = {"US30": "US30.cash", "GOLD": "XAUUSDp"}
    slave.symbol_whitelist = ["EURUSD", "GBPUSD"]
    slave.symbol_blacklist = ["XAUUSD"]
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        assert screen.query_one("#f-symbol_prefix", Input).value == "m."
        assert screen.query_one("#f-symbol_suffix", Input).value == "p"
        assert screen.query_one("#f-symbol_map", TextArea).text == "GOLD=XAUUSDp\nUS30=US30.cash"
        assert screen.query_one("#f-symbol_whitelist", Input).value == "EURUSD, GBPUSD"
        assert screen.query_one("#f-symbol_blacklist", Input).value == "XAUUSD"


async def test_the_symbol_settings_are_saved(app: CopierApp, cfg: AppConfig) -> None:
    from mt5copier import config as config_mod

    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-symbol_suffix", Input).value = "p"
        screen.query_one("#f-symbol_map", TextArea).text = "us30 = US30.cash\nnas100=USTEC"
        screen.query_one("#f-symbol_whitelist", Input).value = "EURUSD, XAUUSD"
        screen.query_one("#f-symbol_blacklist", Input).value = "GBPJPY"
        await pilot.press("ctrl+s")
        await pilot.pause()

        slave = cfg.terminals[TID_SLAVE]
        assert slave.symbol_suffix == "p"
        assert slave.symbol_map == {"US30": "US30.cash", "NAS100": "USTEC"}
        assert slave.symbol_whitelist == ["EURUSD", "XAUUSD"]
        assert slave.symbol_blacklist == ["GBPJPY"]

        reloaded = config_mod.load(app._saved_to).terminals[TID_SLAVE]  # type: ignore[attr-defined]
        assert reloaded.symbol_map == {"US30": "US30.cash", "NAS100": "USTEC"}


async def test_a_bad_map_entry_is_refused_under_the_map(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-symbol_map", TextArea).text = "GOLD"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert "MASTER=BROKER" in str(screen.query_one("#e-symbol_map", Static).render())
        assert cfg.terminals[TID_SLAVE].symbol_map == {}


async def test_ctrl_s_saves_from_inside_the_map(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        area = screen.query_one("#f-symbol_map", TextArea)
        area.focus()
        area.text = "GOLD=XAUUSDp"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].symbol_map == {"GOLD": "XAUUSDp"}


async def test_enter_in_the_map_starts_a_new_line_and_does_not_save(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        area = screen.query_one("#f-symbol_map", TextArea)
        area.focus()
        area.text = "GOLD=XAUUSDp"
        area.cursor_location = area.document.end
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen), "Enter in a multi-line box is a new line"
        assert area.text.count("\n") == 1
        assert cfg.terminals[TID_SLAVE].symbol_map == {}


async def test_tab_leaves_the_map_instead_of_typing_a_tab(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        area = screen.query_one("#f-symbol_map", TextArea)
        area.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is screen.query_one("#f-symbol_whitelist", Input)
        assert "\t" not in area.text


async def test_escape_closes_the_form_from_inside_the_map(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-symbol_map", TextArea).focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


# --- errors sit under their field -------------------------------------------------------------


async def test_a_clean_form_reserves_no_room_for_messages(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        for name in ("risk_percent", "symbol_map", "invert"):
            assert screen.query_one(f"#e-{name}", Static).display is False, name
        assert screen.query_one("#general-error", Static).display is False


async def test_a_message_appears_under_its_field_and_goes_when_fixed(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        box = screen.query_one("#f-risk_percent", Input)
        box.value = "0"
        await pilot.press("ctrl+s")
        await pilot.pause()

        message = screen.query_one("#e-risk_percent", Static)
        assert message.display is True
        assert message.region.y > box.region.y, "the message is under the field, not beside it"

        box.value = "0.5"
        screen._show_errors({})
        assert message.display is False


# --- invert and the guard ---------------------------------------------------------------------


async def test_invert_is_saved_on_a_slave_with_no_copies(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-invert", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].invert is True
        assert not isinstance(app.screen, SettingsScreen)


async def test_invert_is_refused_while_the_slave_has_copies(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        app._engine.open_copies[TID_SLAVE] = 2  # type: ignore[attr-defined]
        on_disk = app._saved_to.read_bytes()  # type: ignore[attr-defined]
        screen.query_one("#f-invert", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen), "a refused form stays open"
        message = screen.query_one("#e-invert", Static)
        assert message.display is True
        assert "2 open copies" in str(message.render())
        assert cfg.terminals[TID_SLAVE].invert is False
        assert app._saved_to.read_bytes() == on_disk  # type: ignore[attr-defined]


async def test_invert_is_refused_when_the_copies_could_not_be_counted(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        app._engine.open_copies[TID_SLAVE] = None  # type: ignore[attr-defined]
        screen.query_one("#f-invert", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].invert is False
        assert "could not be counted" in str(screen.query_one("#e-invert", Static).render())


async def test_the_engines_own_refusal_keeps_the_form_open_and_says_why(
    app: CopierApp, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form checks first, to show the reason beside the field. The engine checks again under
    its own lock, and can refuse something the form let through a moment earlier -- an open that
    slipped in between. That refusal has to reach the user, not vanish."""
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        monkeypatch.setattr(
            app._engine,
            "apply_settings",
            lambda new: "invert: a copy opened a moment ago",
        )
        screen.query_one("#f-invert", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen)
        general = screen.query_one("#general-error", Static)
        assert general.display is True
        assert "a copy opened a moment ago" in str(general.render())
        assert cfg.terminals[TID_SLAVE].invert is False


async def test_a_change_to_something_else_is_not_held_up_by_open_copies(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        app._engine.open_copies[TID_SLAVE] = 5  # type: ignore[attr-defined]
        screen.query_one("#f-risk_percent", Input).value = "0.3"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].risk_percent == 0.3


# --- enabled ------------------------------------------------------------------------------------


async def test_disabling_a_slave_with_open_copies_is_allowed_but_warned(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        app._engine.open_copies[TID_SLAVE] = 2  # type: ignore[attr-defined]
        screen.query_one("#f-enabled", Checkbox).value = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].enabled is False
        assert any("unmanaged" in t and "2 open copies" in t for t in _toasts(app))


async def test_disabling_a_slave_with_nothing_open_is_quiet(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-enabled", Checkbox).value = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].enabled is False
        assert not any("unmanaged" in t for t in _toasts(app))


# --- warnings and the record ---------------------------------------------------------------


async def test_enabling_no_stop_copying_is_flagged(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-no_stop_policy", Select).value = "lot_multiple"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].no_stop_policy is NoStopPolicy.LOT_MULTIPLE
        assert any("UNPROTECTED" in t for t in _toasts(app))


async def test_an_edit_is_written_to_the_log(
    app: CopierApp, caplog: pytest.LogCaptureFixture
) -> None:
    """A risk setting changed while copying is live has to be findable afterwards."""
    import logging

    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-risk_percent", Input).value = "0.2"
        with caplog.at_level(logging.INFO):
            await pilot.press("ctrl+s")
            await pilot.pause()
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "settings changed for Beta Broker" in m and "risk_percent 1 -> 0.2" in m for m in messages
    )


async def test_an_edit_reads_the_live_terminal_at_the_moment_of_saving(
    app: CopierApp, cfg: AppConfig
) -> None:
    """A rescan can change discovery fields while the form is open. The edit is built on what is
    true when it is saved, so it must not put the old values back."""
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        cfg.terminals[TID_SLAVE].last_login = 999_999
        screen.query_one("#f-copy_pending", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].last_login == 999_999
        assert cfg.terminals[TID_SLAVE].copy_pending is True
