"""UI tests via Textual's headless harness.

The Engine is stubbed out, so these tests spawn no processes and touch no MT5 terminal. What
they verify is that key presses and clicks produce the right *config* changes and that the
changes are persisted -- the role assignment rules are the part where a mistake would put real
trades on the wrong account.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, TextArea

from mt5copier.config import AppConfig, NoStopPolicy, Role, SizingMode, TerminalConfig
from mt5copier.copy_stats import TerminalStats
from mt5copier.engine import RecordedAction, RecordedResult, TerminalView
from mt5copier.protocol import (
    COPIER_MAGIC,
    Position,
    PositionLive,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.reconcile import OpenCopy
from mt5copier.settings_edit import copies_word
from mt5copier.store import Orphan
from mt5copier.tui.app import CopierApp
from mt5copier.tui.live_override_screen import LiveOverrideScreen
from mt5copier.tui.orphan_screen import OrphanConfirmScreen
from mt5copier.tui.settings_screen import SettingsScreen
from mt5copier.tui.terminal_screen import TerminalScreen

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
        self.copies: dict[str, list[OpenCopy] | None] = {}
        """What the stub says each slave holds. ``None`` is a read that failed."""
        self.given_up: list[tuple[str, list[int]]] = []
        """Each time the engine was agreed a slave's copies could be given up: (slave, tickets)."""
        self.snapshots: dict[str, Snapshot] = {}
        self.stats: dict[str, TerminalStats] = {}

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
        views = [
            TerminalView(cfg=t.model_copy(deep=True), snapshot=self.snapshots.get(t.terminal_id))
            for t in self._cfg.terminals.values()
        ]
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

    def terminal_stats(self, terminal_id: str) -> TerminalStats:
        return self.stats.get(terminal_id, TerminalStats())

    def open_copies(self, terminal_id: str) -> list[OpenCopy] | None:
        copies = self.copies.get(terminal_id, [])
        return None if copies is None else list(copies)

    def copies_at_stake(
        self, terminal_id: str, new: TerminalConfig | None = None
    ) -> dict[str, list[OpenCopy]] | None:
        """The real engine's rule: a slave stakes its own copies; the master stakes every
        slave's when unassigned (``new`` None) or disabled, and nothing otherwise."""
        cfg = self._cfg.terminals[terminal_id]
        if cfg.role is not Role.MASTER:
            ids = [terminal_id]
        elif new is None or (cfg.enabled and not new.enabled):
            ids = [t.terminal_id for t in self._cfg.slaves]
        else:
            ids = []
        stake: dict[str, list[OpenCopy]] = {}
        for tid in ids:
            held = self.open_copies(tid)
            if held is None:
                return None
            if held:
                stake[tid] = held
        return stake

    def _give_up(
        self, stake: dict[str, list[OpenCopy]] | None, orphan: Mapping[str, Collection[int]] | None
    ) -> str | None:
        """The real engine's contract: copies are only given up when the ones agreed to are
        exactly the ones held now."""
        if stake is None:
            return "its open copies could not be read, so they cannot be given up safely"
        if not stake:
            return None
        if orphan is None:
            total = sum(len(h) for h in stake.values())
            return f"{copies_word(total)} would be orphaned; that has to be confirmed"
        agreed = {t: set(tickets) for t, tickets in orphan.items() if tickets}
        if agreed != {t: {c.master_ticket for c in h} for t, h in stake.items()}:
            return "its open copies changed while you were deciding; try again"
        for tid in stake:
            self.given_up.append((tid, sorted(agreed[tid])))
            self.copies[tid] = []
        return None

    def apply_settings(
        self, new: TerminalConfig, *, orphan: Mapping[str, Collection[int]] | None = None
    ) -> str | None:
        refusal = self._give_up(self.copies_at_stake(new.terminal_id, new), orphan)
        if refusal is not None:
            return refusal
        self._cfg.replace_terminal(new)
        return None

    def set_paused(self, terminal_id: str, paused: bool) -> None:
        cfg = self._cfg.terminals[terminal_id]
        self._cfg.replace_terminal(cfg.model_copy(update={"paused": paused}))

    def unassign(
        self, terminal_id: str, *, orphan: Mapping[str, Collection[int]] | None = None
    ) -> str | None:
        from mt5copier import role_actions

        refusal = self._give_up(self.copies_at_stake(terminal_id), orphan)
        if refusal is not None:
            return refusal
        role_actions.unassign(self._cfg, terminal_id)
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
    """The table row a terminal is drawn on -- section headings take rows too."""
    return app._row_keys.index(terminal_id)


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


async def test_the_master_is_not_replaced_without_unassigning_it_first(
    app: CopierApp, cfg: AppConfig
) -> None:
    """Pressing m on another terminal is refused while there is a master; unassign, then m."""
    async with app.run_test() as pilot:
        await pilot.press("m")
        await pilot.press("down")
        await pilot.press("m")
        assert cfg.terminals[TID_MASTER].role is Role.MASTER
        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED

        app.query_one("#terminals").move_cursor(row=row_of(app, TID_MASTER))
        await pilot.press("u")
        app.query_one("#terminals").move_cursor(row=row_of(app, TID_SLAVE))
        await pilot.press("m")
        assert cfg.terminals[TID_SLAVE].role is Role.MASTER


async def test_pressing_s_sets_slave_and_does_not_toggle(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        cfg.set_role(TID_SLAVE, Role.MASTER)  # a slave needs a master to exist first
        await pilot.press("s")
        assert cfg.terminals[TID_MASTER].role is Role.SLAVE
        await pilot.press("s")
        assert cfg.terminals[TID_MASTER].role is Role.SLAVE


async def test_question_mark_opens_and_closes_the_key_list(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen.query("HelpPanel")
        await pilot.press("question_mark")
        await pilot.pause()
        assert not app.screen.query("HelpPanel")
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not app.screen.query("HelpPanel")


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


async def _click_cell(app: CopierApp, pilot: object, terminal_id: str, column: str) -> None:
    """A real mouse click on that terminal's cell, at the screen position the table drew it.

    Real, not a posted message: a row-cursor DataTable never sends CellSelected, so the only
    honest test of a clickable cell is a click.
    """
    from mt5copier.tui.app import _COLUMNS, TerminalTable

    table = app.query_one("#terminals", TerminalTable)
    x = 1  # the border
    for key, _, width in _COLUMNS:
        if key == column:
            break
        x += width + 2 * table.cell_padding
    x += table.cell_padding + 1
    y = 1 + 1 + row_of(app, terminal_id)  # border, header, row
    assert await pilot.click(TerminalTable, offset=(x, y))  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


async def test_with_no_master_every_row_offers_master(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(180, 40)) as pilot:
        await _click_cell(app, pilot, TID_SLAVE, "action")
        assert cfg.terminals[TID_SLAVE].role is Role.MASTER


async def test_with_a_master_the_other_rows_offer_slave(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(180, 40)) as pilot:
        await _click_cell(app, pilot, TID_MASTER, "action")
        assert cfg.terminals[TID_MASTER].role is Role.MASTER
        await _click_cell(app, pilot, TID_SLAVE, "action")
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE
        assert cfg.terminals[TID_MASTER].role is Role.MASTER, "a click never replaces the master"


async def test_an_assigned_rows_action_is_unassign(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(180, 40)) as pilot:
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        app._refresh()
        await _click_cell(app, pilot, TID_SLAVE, "action")
        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED
        await _click_cell(app, pilot, TID_MASTER, "action")
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED


async def test_the_gear_opens_settings(app: CopierApp) -> None:
    async with app.run_test(size=(180, 40)) as pilot:
        await _click_cell(app, pilot, TID_SLAVE, "settings")
        assert isinstance(app.screen, SettingsScreen)


async def test_clicking_other_columns_does_not_change_roles(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(180, 40)) as pilot:
        await _click_cell(app, pilot, TID_SLAVE, "terminal")
        await _click_cell(app, pilot, TID_SLAVE, "status")
        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED


async def test_the_table_is_grouped_by_role(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        await pilot.press("m")
        keys = app._row_keys
        assert keys.index(TID_MASTER) < keys.index("§slaves") < keys.index("§unassigned")
        assert keys.index("§unassigned") < keys.index(TID_SLAVE)


async def test_the_cursor_steps_over_section_rows(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        assert app._selected_id() == TID_MASTER, "it starts on a terminal, not a heading"
        await pilot.press("m")
        assert app._selected_id() == TID_MASTER, "the cursor followed it into its new group"
        await pilot.press("down")
        await pilot.pause()
        assert app._selected_id() == TID_SLAVE
        await pilot.press("up")
        await pilot.pause()
        assert app._selected_id() == TID_MASTER


async def test_the_log_page_shows_the_log_file(
    app: CopierApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import RichLog

    (tmp_path / "copier.log").write_text(
        "2026-10-02 00:34:02 WARNING [MainProcess] x: something to see\n", encoding="utf-8"
    )
    monkeypatch.setattr("mt5copier.tui.app.log_dir", lambda: tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("2")
        await pilot.pause()
        panel = app.query_one("#log", RichLog)
        assert any("something to see" in line.text for line in panel.lines)
        await pilot.press("1")
        assert app.focused is app.query_one("#terminals")


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
    """Make ``terminal_id`` a slave, select it, and open its settings.

    Needs a master to exist first -- ``role_actions.set_slave`` refuses to create a slave
    otherwise -- so this gives TID_MASTER the role directly, the same way other tests here reach
    into ``cfg``/``app._cfg`` for a precondition rather than drive it through a keypress.
    """
    app._cfg.set_role(TID_MASTER, Role.MASTER)  # type: ignore[attr-defined]
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


async def test_the_help_panel_follows_focus_and_stays_on_screen(app: CopierApp) -> None:
    """Help for the field being edited is titled with its name, and is drawn even when that
    field is far down a form too tall for the terminal."""
    import html

    from mt5copier.settings_edit import EDITABLE

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_editor(app, pilot)
        help_panel = screen.query_one("#help", Static)
        assert help_panel.border_title == EDITABLE[0].label

        last = EDITABLE[-1]
        screen.query_one(f"#f-{last.name}").focus()
        await pilot.pause()
        assert help_panel.border_title == last.label
        drawn = html.unescape(app.export_screenshot()).replace("\xa0", " ")
        assert last.label in drawn
        assert last.help.split()[0] in drawn


def _connected_views(app: CopierApp, *, trade_mode: int) -> None:
    """Make the stub engine report every terminal connected, on an account of this trade mode."""
    import time

    from mt5copier.protocol import Snapshot, WorkerState

    stub_views = app._engine.views

    def views() -> list[TerminalView]:
        out = []
        for v in stub_views():
            v.supervised = True
            v.process_alive = True
            v.received_at = time.monotonic()
            v.snapshot = Snapshot(
                terminal_id=v.cfg.terminal_id, state=WorkerState.CONNECTED, trade_mode=trade_mode
            )
            out.append(v)
        return out

    app._engine.views = views  # type: ignore[method-assign]


async def test_a_live_slave_held_by_the_demo_guard_looks_blocked_and_says_press_l(
    app: CopierApp, cfg: AppConfig
) -> None:
    from mt5copier.tui.app import _ICON_BLOCKED, TerminalTable

    async with app.run_test() as pilot:
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        _connected_views(app, trade_mode=2)  # real money
        app._refresh()
        app.query_one("#terminals").move_cursor(row=row_of(app, TID_SLAVE))
        await pilot.pause()

        table = app.query_one("#terminals", TerminalTable)
        assert _ICON_BLOCKED in str(table.get_cell(TID_SLAVE, "terminal"))
        assert "[BLOCKED] press l" in str(table.get_cell(TID_SLAVE, "status"))
        assert "BLOCKED" not in str(table.get_cell(TID_MASTER, "status")), "masters are not held"
        assert "BLOCKED 1" in _statusbar(app)
        assert "Press l" in str(app.query_one("#detail", Static).render())

        cfg.terminals[TID_SLAVE].allow_live_trading = True
        app._refresh()
        assert "BLOCKED" not in str(table.get_cell(TID_SLAVE, "status"))
        assert "BLOCKED" not in _statusbar(app)


async def test_a_demo_slave_is_not_blocked(app: CopierApp, cfg: AppConfig) -> None:
    from mt5copier.tui.app import TerminalTable

    async with app.run_test():
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        _connected_views(app, trade_mode=0)  # demo
        app._refresh()
        table = app.query_one("#terminals", TerminalTable)
        assert "[OK]" in str(table.get_cell(TID_SLAVE, "status"))
        assert "BLOCKED" not in _statusbar(app)


async def test_the_form_shows_the_overrides_state(app: CopierApp, cfg: AppConfig) -> None:
    cfg.terminals[TID_SLAVE].allow_live_trading = True
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        note = str(screen.query_one("#override", Static).render())
        assert "Live trading" in note
        assert ": on" in note


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


# --- open copies: asked about, then given up ------------------------------------------------
#
# An edit to a terminal that holds copies stops the copier managing them, for every setting alike,
# so it is asked about first. The question lists what would be given up and rests on Cancel, and
# nothing has happened until it is answered: declining leaves the form open, the edit still typed
# in, and the copies tracked.


def copy_of(
    master_ticket: int,
    slave_ticket: int | None = None,
    symbol: str = "EURGBPp",
    volume: float = 0.5,
    resting: bool = False,
) -> OpenCopy:
    ticket = master_ticket + 1000 if slave_ticket is None else slave_ticket
    return OpenCopy(master_ticket, ticket, symbol, volume, resting)


def hold(app: CopierApp, *copies: OpenCopy, terminal_id: str = TID_SLAVE) -> None:
    app._engine.copies[terminal_id] = list(copies)  # type: ignore[attr-defined]


def _text(screen: object, selector: str) -> str:
    return str(screen.query_one(selector, Static).render())  # type: ignore[attr-defined]


async def _save_with_copies(
    app: CopierApp, pilot: object, *copies: OpenCopy
) -> tuple[SettingsScreen, OrphanConfirmScreen]:
    """Open the form, change a setting, save it, and return both screens: the form, and the
    question it provoked."""
    form = await _open_editor(app, pilot)
    hold(app, *copies)
    form.query_one("#f-risk_percent", Input).value = "0.3"
    await pilot.press("ctrl+s")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    question = app.screen
    assert isinstance(question, OrphanConfirmScreen)
    return form, question


async def test_a_slave_with_no_copies_saves_without_being_asked(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-invert", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].invert is True
        assert not isinstance(app.screen, SettingsScreen | OrphanConfirmScreen)


async def test_saving_over_open_copies_asks_first_and_names_them(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test(size=(100, 40)) as pilot:
        _, question = await _save_with_copies(
            app,
            pilot,
            copy_of(11, 900, "EURGBPp", 13.77),
            copy_of(12, 901, "EURUSDp", 0.5, resting=True),
        )
        assert "Orphan 2 open copies on Beta Broker" in _text(question, "#title")
        listing = _text(question, "#copies")
        assert "#900" in listing and "EURGBPp" in listing and "13.77" in listing
        assert "#901" in listing and "resting order" in listing
        assert "stay open" in _text(question, "#consequence")
        assert "risk_percent 1 -> 0.3" in _text(question, "#changes")
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0, "nothing has happened yet"
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_the_question_rests_on_cancel(app: CopierApp) -> None:
    """Confirming abandons real positions, so the resting choice is the one that changes nothing."""
    async with app.run_test(size=(100, 40)) as pilot:
        _, question = await _save_with_copies(app, pilot, copy_of(11))
        assert app.focused is question.query_one("#cancel")


async def test_enter_on_the_question_declines(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(100, 40)) as pilot:
        await _save_with_copies(app, pilot, copy_of(11))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_declining_returns_to_the_form_with_nothing_changed(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test(size=(100, 40)) as pilot:
        form, _ = await _save_with_copies(app, pilot, copy_of(11))
        on_disk = app._saved_to.read_bytes()  # type: ignore[attr-defined]

        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen), "the form is still open"
        assert form.query_one("#f-risk_percent", Input).value == "0.3", "with the edit still typed"
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0
        assert app._engine.given_up == []  # type: ignore[attr-defined]
        assert app._engine.copies[TID_SLAVE], "the copies are still tracked"  # type: ignore[attr-defined]
        assert app._saved_to.read_bytes() == on_disk  # type: ignore[attr-defined]
        assert not any("orphan" in t for t in _toasts(app))


async def test_agreeing_gives_the_copies_up_applies_the_edit_and_says_so(
    app: CopierApp, cfg: AppConfig
) -> None:
    from mt5copier import config as config_mod

    cfg.dry_run = False
    async with app.run_test(size=(100, 40)) as pilot:
        _, question = await _save_with_copies(app, pilot, copy_of(11, 900), copy_of(12, 901))

        question.query_one("#go", Button).press()
        await pilot.pause()

        assert cfg.terminals[TID_SLAVE].risk_percent == 0.3
        assert app._engine.given_up == [(TID_SLAVE, [11, 12])]  # type: ignore[attr-defined]
        assert not isinstance(app.screen, SettingsScreen | OrphanConfirmScreen), "both close"
        reloaded = config_mod.load(app._saved_to)  # type: ignore[attr-defined]
        assert reloaded.terminals[TID_SLAVE].risk_percent == 0.3
        (said,) = [t for t in _toasts(app) if "orphaned" in t]
        assert "2 open copies" in said and "#900" in said and "#901" in said
        assert "by hand" in said, "and what it means for them"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("f-risk_percent", "0.3"),
        ("f-max_lot", "2"),
        ("f-symbol_suffix", "p"),
        ("f-invert", True),
        ("f-copy_sltp", False),
        ("f-copy_pending", True),
    ],
)
async def test_every_setting_is_asked_about_alike(
    app: CopierApp, field: str, value: object
) -> None:
    """Which settings are safe under an open copy is not something a person should have to know."""
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await _open_editor(app, pilot)
        hold(app, copy_of(11))
        screen.query_one(f"#{field}").value = value  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, OrphanConfirmScreen), field


async def test_saving_a_form_that_changed_nothing_asks_nothing(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        await _open_editor(app, pilot)
        hold(app, copy_of(11))
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, OrphanConfirmScreen)
        assert any("no changes" in t for t in _toasts(app))


async def test_a_slave_whose_copies_cannot_be_read_is_refused_and_not_changed(
    app: CopierApp, cfg: AppConfig
) -> None:
    """'We could not tell' is not a reason to change a slave that may be holding copies."""
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        app._engine.copies[TID_SLAVE] = None  # type: ignore[attr-defined]
        screen.query_one("#f-risk_percent", Input).value = "0.3"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen)
        general = screen.query_one("#general-error", Static)
        assert general.display is True
        assert "could not be read" in str(general.render())
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0


async def test_a_refusal_after_agreeing_reaches_the_form(app: CopierApp, cfg: AppConfig) -> None:
    """A copy opened while the person was deciding is one they were not told about. The engine
    checks what they agreed to against what is held now, and the refusal has to be seen."""
    async with app.run_test(size=(100, 40)) as pilot:
        form, question = await _save_with_copies(app, pilot, copy_of(11))
        hold(app, copy_of(11), copy_of(13))

        question.query_one("#go", Button).press()
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen)
        general = form.query_one("#general-error", Static)
        assert general.display is True
        assert "changed while you were deciding" in str(general.render())
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_the_engines_own_refusal_keeps_the_form_open_and_says_why(
    app: CopierApp, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        monkeypatch.setattr(
            app._engine, "apply_settings", lambda new, orphan=None: "a copy opened a moment ago"
        )
        screen.query_one("#f-risk_percent", Input).value = "0.3"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen)
        general = screen.query_one("#general-error", Static)
        assert general.display is True
        assert "a copy opened a moment ago" in str(general.render())
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0


async def test_the_panic_key_works_from_the_question(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(100, 40)) as pilot:
        await _save_with_copies(app, pilot, copy_of(11))
        cfg.copying_enabled = True
        await pilot.press("f9")
        await pilot.pause()
        assert cfg.copying_enabled is False
        assert not isinstance(app.screen, OrphanConfirmScreen)
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0, "and it agreed to nothing"
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_a_dry_run_says_the_copies_are_simulated(app: CopierApp, cfg: AppConfig) -> None:
    cfg.dry_run = True
    async with app.run_test(size=(100, 40)) as pilot:
        _, question = await _save_with_copies(app, pilot, copy_of(11))
        assert "simulated" in _text(question, "#dryrun")
        question.query_one("#go", Button).press()
        await pilot.pause()
        (said,) = [t for t in _toasts(app) if "simulated" in t]
        assert "by hand" not in said, "there is nothing real to close"


async def test_a_live_session_does_not_call_the_copies_simulated(
    app: CopierApp, cfg: AppConfig
) -> None:
    cfg.dry_run = False
    async with app.run_test(size=(100, 40)) as pilot:
        _, question = await _save_with_copies(app, pilot, copy_of(11))
        assert not question.query("#dryrun")


async def test_a_long_list_is_cut_but_the_count_is_whole(app: CopierApp) -> None:
    async with app.run_test(size=(100, 60)) as pilot:
        _, question = await _save_with_copies(app, pilot, *(copy_of(i) for i in range(20, 32)))
        assert "Orphan 12 open copies" in _text(question, "#title")
        listing = _text(question, "#copies")
        assert "#1027" in listing and "#1028" not in listing
        assert "and 4 more" in listing


async def test_the_question_is_usable_on_an_80_by_24_terminal(app: CopierApp) -> None:
    """The button has to be on screen and what it consents to has to be drawn, not clipped."""
    import html

    async with app.run_test(size=(80, 24)) as pilot:
        _, question = await _save_with_copies(
            app, pilot, copy_of(11), copy_of(12), copy_of(13), copy_of(14)
        )
        question.query_one("#go").focus()
        await pilot.pause(0.3)
        region = question.query_one("#go").region
        assert region.y >= 0
        assert region.y + region.height <= 24
        drawn = html.unescape(app.export_screenshot()).replace("\xa0", " ")
        assert "Orphan them and apply" in drawn


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


# --- the live-trading override ---------------------------------------------------------------------
#
# The demo-only guard's override has its own key and its own dialog, and turning it ON has to be
# asked for by typing the terminal's name. The wrong-terminal mistake is the one being guarded
# against, so the useful failing case is the name of a *different* terminal.


async def _open_override(
    app: CopierApp, pilot: object, terminal_id: str = TID_SLAVE, *, role_key: str | None = "s"
) -> LiveOverrideScreen:
    """Select ``terminal_id``, give it a role with ``role_key`` (None keeps its own), press l."""
    if role_key == "s":
        # Same precondition _open_editor needs: a slave can't be created with no master.
        app._cfg.set_role(TID_MASTER, Role.MASTER)  # type: ignore[attr-defined]
    app.query_one("#terminals").move_cursor(row=row_of(app, terminal_id))
    if role_key is not None:
        await pilot.press(role_key)  # type: ignore[attr-defined]
    await pilot.press("l")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    screen = app.screen
    assert isinstance(screen, LiveOverrideScreen)
    return screen


def _statusbar(app: CopierApp) -> str:
    return str(app.query_one("#statusbar", Static).render())


async def test_l_opens_the_override_for_the_selected_terminal(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        assert "Beta Broker" in str(screen.query_one("#title", Static).render())
        assert "OFF" in str(screen.query_one("#state", Static).render())
        assert "Beta Broker" in str(screen.query_one("#prompt", Static).render())
        assert app.focused is screen.query_one("#typed"), "it starts in the box"


async def test_typing_the_name_turns_it_on_and_it_reaches_the_config_the_disk_and_the_engine(
    app: CopierApp, cfg: AppConfig
) -> None:
    from mt5copier import config as config_mod

    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        syncs = app._engine.sync_calls  # type: ignore[attr-defined]

        screen.query_one("#typed", Input).value = "Beta Broker"
        await pilot.press("enter")
        await pilot.pause()

        assert cfg.terminals[TID_SLAVE].allow_live_trading is True
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE, "and nothing else about it moved"
        assert not isinstance(app.screen, LiveOverrideScreen), "a confirmed dialog closes"
        assert app._engine.sync_calls > syncs  # type: ignore[attr-defined]
        reloaded = config_mod.load(app._saved_to)  # type: ignore[attr-defined]
        assert reloaded.terminals[TID_SLAVE].allow_live_trading is True
        assert any("prop" in t for t in _toasts(app)), "lifting the guard is said out loud"


async def test_the_button_does_the_same_as_enter(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        screen.query_one("#typed", Input).value = "beta broker"
        screen.query_one("#go", Button).press()
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].allow_live_trading is True


async def test_the_name_of_a_different_terminal_is_refused(app: CopierApp, cfg: AppConfig) -> None:
    """The mistake the whole dialog exists for: the wrong terminal selected, and a name typed
    from memory that belongs to the right one."""
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        screen.query_one("#typed", Input).value = "Alpha Broker"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, LiveOverrideScreen), "the dialog stays open"
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False
        error = screen.query_one("#error", Static)
        assert error.display
        assert "Beta Broker" in str(error.render()), "and says what would have been accepted"


async def test_pressing_the_button_with_nothing_typed_is_refused(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        screen.query_one("#go", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, LiveOverrideScreen)
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False
        assert screen.query_one("#error", Static).display


async def test_a_terminal_that_is_not_a_slave_cannot_be_lifted_even_with_its_name(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot, TID_MASTER, role_key="m")
        screen.query_one("#typed", Input).value = "Alpha Broker"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LiveOverrideScreen)
        assert cfg.terminals[TID_MASTER].allow_live_trading is False
        assert "slave" in str(screen.query_one("#error", Static).render())


async def test_a_refusal_writes_nothing_to_disk(app: CopierApp, cfg: AppConfig) -> None:
    from mt5copier import config as config_mod

    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        screen.query_one("#typed", Input).value = "wrong"
        await pilot.press("enter")
        await pilot.pause()
        reloaded = config_mod.load(app._saved_to)  # type: ignore[attr-defined]
        assert reloaded.terminals[TID_SLAVE].allow_live_trading is False


async def test_escape_discards_what_was_typed(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        screen.query_one("#typed", Input).value = "Beta Broker"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, LiveOverrideScreen)
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False


async def test_keys_typed_in_the_box_do_not_reach_the_app(app: CopierApp, cfg: AppConfig) -> None:
    """m, s, u, d and c act on the selected terminal, and a name is full of letters."""
    async with app.run_test() as pilot:
        await _open_override(app, pilot)
        roles = {t: c.role for t, c in cfg.terminals.items()}
        for key in ("m", "s", "u", "d", "c", "e", "l"):
            await pilot.press(key)
        assert {t: c.role for t, c in cfg.terminals.items()} == roles
        assert cfg.copying_enabled is False
        assert app._engine.reconnected == []  # type: ignore[attr-defined]
        assert isinstance(app.screen, LiveOverrideScreen)


async def test_the_panic_key_still_works_with_the_dialog_open(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        cfg.copying_enabled = True
        screen.query_one("#typed", Input).value = "Beta Broker"
        await pilot.press("f9")
        await pilot.pause()
        assert cfg.copying_enabled is False
        assert not isinstance(app.screen, LiveOverrideScreen)
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False, "what was typed is dropped"


async def test_turning_it_off_takes_one_press_and_no_typing(app: CopierApp, cfg: AppConfig) -> None:
    """The safe direction. There is no box to type in, and Enter on the button does it."""
    cfg.set_role(TID_SLAVE, Role.SLAVE)
    cfg.terminals[TID_SLAVE].allow_live_trading = True
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot, role_key=None)
        assert not screen.query("#typed"), "nothing to type"
        assert "ON" in str(screen.query_one("#state", Static).render())
        assert app.focused is screen.query_one("#go")
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False
        assert not isinstance(app.screen, LiveOverrideScreen)


async def test_turning_it_off_works_after_the_terminal_stopped_being_a_slave(
    app: CopierApp, cfg: AppConfig
) -> None:
    """The guard stays lifted across a role change. It must still be possible to put it back."""
    cfg.terminals[TID_SLAVE].allow_live_trading = True
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot, role_key=None)
        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False
        assert not screen.is_attached


async def test_the_override_dialog_asks_about_open_copies_too(
    app: CopierApp, cfg: AppConfig
) -> None:
    """It is a settings change like any other, so the same question comes first."""
    cfg.dry_run = False
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await _open_override(app, pilot)
        hold(app, copy_of(11))
        screen.query_one("#typed", Input).value = "Beta Broker"
        await pilot.press("enter")
        await pilot.pause()

        question = app.screen
        assert isinstance(question, OrphanConfirmScreen)
        assert "allow_live_trading off -> on" in _text(question, "#changes")
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False, "not yet"

        question.query_one("#go", Button).press()
        await pilot.pause()

        assert cfg.terminals[TID_SLAVE].allow_live_trading is True
        assert app._engine.given_up == [(TID_SLAVE, [11])]  # type: ignore[attr-defined]
        assert not isinstance(app.screen, LiveOverrideScreen | OrphanConfirmScreen)


async def test_declining_leaves_the_override_dialog_open_and_the_guard_where_it_was(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await _open_override(app, pilot)
        hold(app, copy_of(11))
        screen.query_one("#typed", Input).value = "Beta Broker"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, LiveOverrideScreen)
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_putting_the_guard_back_over_open_copies_is_asked_about_too(
    app: CopierApp, cfg: AppConfig
) -> None:
    cfg.set_role(TID_SLAVE, Role.SLAVE)
    cfg.terminals[TID_SLAVE].allow_live_trading = True
    async with app.run_test(size=(100, 40)) as pilot:
        await _open_override(app, pilot, role_key=None)
        hold(app, copy_of(11), copy_of(12))
        await pilot.press("enter")
        await pilot.pause()

        question = app.screen
        assert isinstance(question, OrphanConfirmScreen)
        assert "Orphan 2 open copies" in _text(question, "#title")

        question.query_one("#go", Button).press()
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False


async def test_turning_it_off_with_nothing_open_is_not_asked_about(
    app: CopierApp, cfg: AppConfig
) -> None:
    cfg.set_role(TID_SLAVE, Role.SLAVE)
    cfg.terminals[TID_SLAVE].allow_live_trading = True
    async with app.run_test() as pilot:
        await _open_override(app, pilot, role_key=None)
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, OrphanConfirmScreen)
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False


async def test_a_change_to_the_override_is_logged_as_a_warning_in_both_directions(
    app: CopierApp, caplog: pytest.LogCaptureFixture
) -> None:
    """A guard that can be lifted has to leave a mark that is easy to find afterwards, and an
    INFO line among the risk edits is not one."""
    import logging

    async with app.run_test() as pilot:
        with caplog.at_level(logging.INFO):
            screen = await _open_override(app, pilot)
            screen.query_one("#typed", Input).value = "Beta Broker"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
    records = [r for r in caplog.records if "settings changed for Beta Broker" in r.getMessage()]
    assert [r.getMessage().split(": ", 1)[1] for r in records] == [
        "allow_live_trading off -> on",
        "allow_live_trading on -> off",
    ]
    assert all(r.levelno == logging.WARNING for r in records)


async def test_an_ordinary_edit_is_still_logged_at_info(
    app: CopierApp, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-risk_percent", Input).value = "0.2"
        with caplog.at_level(logging.INFO):
            await pilot.press("ctrl+s")
            await pilot.pause()
    (record,) = [r for r in caplog.records if "settings changed for Beta Broker" in r.getMessage()]
    assert record.levelno == logging.INFO


async def test_the_status_bar_says_which_terminals_have_the_guard_lifted(
    app: CopierApp, cfg: AppConfig
) -> None:
    """Said on the main screen, not only in the dialog. It also covers a terminal that is not a
    slave, because the flag survives a role change and would otherwise be invisible."""
    async with app.run_test():
        app._refresh()
        assert "NON-DEMO" not in _statusbar(app)

        cfg.terminals[TID_SLAVE].allow_live_trading = True
        app._refresh()
        assert "NON-DEMO ALLOWED: Beta Broker" in _statusbar(app)

        cfg.terminals[TID_MASTER].allow_live_trading = True
        app._refresh()
        text = _statusbar(app)
        assert "Beta Broker" in text and "Alpha Broker" in text

        cfg.terminals[TID_SLAVE].allow_live_trading = False
        cfg.terminals[TID_MASTER].allow_live_trading = False
        app._refresh()
        assert "NON-DEMO" not in _statusbar(app)


async def test_the_status_bar_follows_the_dialog(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        screen.query_one("#typed", Input).value = "Beta Broker"
        await pilot.press("enter")
        await pilot.pause()
        app._refresh()
        assert "NON-DEMO ALLOWED: Beta Broker" in _statusbar(app)

        await pilot.press("l")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app._refresh()
        assert "NON-DEMO" not in _statusbar(app)


async def test_the_settings_form_points_at_the_key_for_the_override(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        note = str(screen.query_one("#override", Static).render())
        assert "with l" in note
        assert "config.json" not in note, "it is no longer a hand edit"


async def test_the_override_dialog_is_usable_on_an_80_by_24_terminal(app: CopierApp) -> None:
    """The button has to be on screen, and what it asks the user to type has to be drawn."""
    import html

    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open_override(app, pilot)
        screen.query_one("#go").focus()
        await pilot.pause(0.3)
        region = screen.query_one("#go").region
        assert region.y >= 0
        assert region.y + region.height <= 24
        drawn = html.unescape(app.export_screenshot()).replace("\xa0", " ")
        assert "Turn ON" in drawn


# --- unassigning over open copies asks first --------------------------------------------------


async def _press_unassign_on(app: CopierApp, pilot: object, terminal_id: str) -> None:
    app.query_one("#terminals").move_cursor(row=row_of(app, terminal_id))
    await pilot.press("u")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


async def test_unassigning_a_slave_with_copies_asks_and_cancel_changes_nothing(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test(size=(100, 40)) as pilot:
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        app._refresh()
        app._engine.copies[TID_SLAVE] = [copy_of(11, 900, "EURGBPp", 1.5)]  # type: ignore[attr-defined]
        await _press_unassign_on(app, pilot, TID_SLAVE)

        question = app.screen
        assert isinstance(question, OrphanConfirmScreen)
        assert "Orphan 1 open copy on Beta Broker" in _text(question, "#title")
        assert "#900" in _text(question, "#copies")
        assert "Unassigning" in _text(question, "#lead")
        assert "unassign" in str(question.query_one("#go", Button).label).lower()

        await pilot.press("escape")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_agreeing_unassigns_the_slave_and_says_what_to_close(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test(size=(100, 40)) as pilot:
        cfg.dry_run = False
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        app._refresh()
        app._engine.copies[TID_SLAVE] = [copy_of(11, 900)]  # type: ignore[attr-defined]
        await _press_unassign_on(app, pilot, TID_SLAVE)

        app.screen.query_one("#go", Button).press()
        await pilot.pause()

        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED
        assert app._engine.given_up == [(TID_SLAVE, [11])]  # type: ignore[attr-defined]
        assert any("#900" in t and "close them by hand" in t for t in _toasts(app))


async def test_unassigning_the_master_asks_about_every_slaves_copies(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test(size=(100, 40)) as pilot:
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        app._refresh()
        app._engine.copies[TID_SLAVE] = [copy_of(11, 900)]  # type: ignore[attr-defined]
        await _press_unassign_on(app, pilot, TID_MASTER)

        question = app.screen
        assert isinstance(question, OrphanConfirmScreen)
        assert "Beta Broker" in _text(question, "#title"), "named by the slave holding them"
        assert "Beta Broker" in _text(question, "#changes"), "and it says the slave goes too"

        question.query_one("#go", Button).press()
        await pilot.pause()
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED
        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED
        assert app._engine.given_up == [(TID_SLAVE, [11])]  # type: ignore[attr-defined]


async def test_unassigning_with_nothing_open_asks_nothing(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        cfg.set_role(TID_MASTER, Role.MASTER)
        app._refresh()
        await _press_unassign_on(app, pilot, TID_MASTER)
        assert not isinstance(app.screen, OrphanConfirmScreen)
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED


async def test_the_unassign_button_asks_too(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(180, 40)) as pilot:
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        app._refresh()
        app._engine.copies[TID_SLAVE] = [copy_of(11, 900)]  # type: ignore[attr-defined]
        await _click_cell(app, pilot, TID_SLAVE, "action")
        assert isinstance(app.screen, OrphanConfirmScreen)
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE


# --- the terminal detail page ----------------------------------------------------------------


def _detail_text(app: CopierApp, widget_id: str) -> str:
    return str(app.screen.query_one(widget_id, Static).render())


def _wire_copying(app: CopierApp, cfg: AppConfig) -> None:
    """A master with one position, and a slave holding its copy plus an unmanaged one."""
    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    engine = app._engine
    engine.snapshots[TID_MASTER] = Snapshot(  # type: ignore[attr-defined]
        terminal_id=TID_MASTER,
        state=WorkerState.CONNECTED,
        balance=10_000.0,
        equity=10_120.0,
        currency="USD",
        open_positions=(
            Position(
                ticket=100,
                symbol="EURUSD",
                type=PositionType.BUY,
                volume=1.0,
                price_open=1.08500,
                sl=1.08000,
            ),
        ),
        live_positions=(PositionLive(ticket=100, price_current=1.08620, profit=120.0),),
    )
    engine.snapshots[TID_SLAVE] = Snapshot(  # type: ignore[attr-defined]
        terminal_id=TID_SLAVE,
        state=WorkerState.CONNECTED,
        trade_mode=0,
        balance=5_000.0,
        equity=5_040.0,
        margin=500.0,
        margin_level=1_008.0,
        margin_free=4_540.0,
        currency="USD",
        ping_last_us=41_250,
        open_positions=(
            Position(
                ticket=5001,
                symbol="EURUSD",
                type=PositionType.BUY,
                volume=0.5,
                price_open=1.08504,
                sl=1.08000,
                magic=COPIER_MAGIC,
            ),
            Position(
                ticket=6000, symbol="GBPUSD", type=PositionType.SELL, volume=0.2, price_open=1.27000
            ),
        ),
        live_positions=(
            PositionLive(ticket=5001, price_current=1.08620, profit=58.0, swap=-0.4),
            PositionLive(ticket=6000, price_current=1.27100, profit=-20.0),
        ),
    )
    engine.copies[TID_SLAVE] = [  # type: ignore[attr-defined]
        OpenCopy(master_ticket=100, slave_ticket=5001, symbol="EURUSD", volume=0.5, resting=False)
    ]
    engine.stats[TID_SLAVE] = TerminalStats(  # type: ignore[attr-defined]
        latencies_ms=(120.0, 80.0, 95.0),
        slippage_points=(4.0,),
        succeeded=3,
        refused=1,
        connected_for_s=125.0,
    )


async def _open(app: CopierApp, pilot: object, terminal_id: str) -> TerminalScreen:
    app._refresh()
    table = app.query_one("#terminals")
    table.move_cursor(row=row_of(app, terminal_id))  # type: ignore[attr-defined]
    table.focus()
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    assert isinstance(app.screen, TerminalScreen)
    return app.screen


async def test_enter_opens_the_terminal_page_and_escape_returns(
    app: CopierApp, cfg: AppConfig
) -> None:
    _wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await _open(app, pilot, TID_SLAVE)
        assert screen.terminal_id == TID_SLAVE
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, TerminalScreen)
        assert app.focused is app.query_one("#terminals")


async def test_the_slave_page_shows_positions_risk_and_health(
    app: CopierApp, cfg: AppConfig
) -> None:
    _wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await _open(app, pilot, TID_SLAVE)
        header = _detail_text(app, "#td-header")
        assert "Beta Broker" in header
        assert "DEMO" in header
        assert "41 ms" in header
        assert "+$40.00" in header

        account = _detail_text(app, "#td-account")
        assert "10%" in account, "500 of 5,040 equity is in use"
        assert "1,008%" in account
        assert "EURUSD" in account and "GBPUSD" in account

        health = _detail_text(app, "#td-health")
        assert "median 95 ms" in health
        assert "+4.0" in health
        assert "3 ok" in health and "1 refused" in health
        assert "2m 05s" in health

        table = screen.query_one("#td-positions")
        assert table.row_count == 2  # type: ignore[attr-defined]
        links = [str(table.get_row_at(i)[-1]) for i in range(2)]  # type: ignore[attr-defined]
        assert links == ["copy of #100 (master 1 lots)", "manual / not ours"]


async def test_the_master_page_counts_slaves_and_has_no_copy_health(
    app: CopierApp, cfg: AppConfig
) -> None:
    _wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await _open(app, pilot, TID_MASTER)
        table = screen.query_one("#td-positions")
        assert str(table.get_row_at(0)[-1]) == "on 1/1 slaves"  # type: ignore[attr-defined]
        assert "nothing to time" in _detail_text(app, "#td-health")


async def test_a_terminal_with_no_snapshot_opens_without_crashing(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await _open(app, pilot, TID_BROKEN)
        assert "not connected" in _detail_text(app, "#td-account")


async def test_clicking_a_row_never_opens_the_page(app: CopierApp) -> None:
    """Only Enter does: the ACTIONS and gear cells are buttons, and a double click on one
    must not also navigate away."""
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.click("#terminals", offset=(5, 3))
        await pilot.click("#terminals", offset=(5, 3))
        await pilot.pause()
        assert not isinstance(app.screen, TerminalScreen)


async def test_the_footer_names_enter_on_the_terminal_table(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        app.query_one("#terminals").focus()
        await pilot.pause()
        enter = app.active_bindings["enter"].binding
        assert enter.show and enter.description == "Details"


async def test_e_on_the_terminal_page_opens_that_terminals_settings_and_esc_comes_back(
    app: CopierApp, cfg: AppConfig
) -> None:
    _wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        await _open(app, pilot, TID_SLAVE)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen._current().terminal_id == TID_SLAVE  # type: ignore[attr-defined]
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, TerminalScreen), "closing the form returns to the page"


async def test_the_master_has_no_settings_form(app: CopierApp) -> None:
    """Everything in the form is read off a slave. The master's gear is gone, and e says why."""
    async with app.run_test(size=(160, 50)) as pilot:
        app._cfg.set_role(TID_MASTER, Role.MASTER)  # type: ignore[attr-defined]
        app._refresh()
        app.query_one("#terminals").move_cursor(row=row_of(app, TID_MASTER))
        await pilot.press("e")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)
        assert any("no settings" in t for t in _toasts(app))
        table = app.query_one("#terminals", DataTable)
        assert str(table.get_cell(TID_MASTER, "settings")) == ""
        assert str(table.get_cell(TID_MASTER, "pause")) == ""


async def test_the_form_no_longer_offers_enabled(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await _open_editor(app, pilot)
        assert not screen.query("#f-enabled")
        assert screen.query("#f-risk_percent")


# --- pausing a slave ------------------------------------------------------------------------


async def _with_slave(app: CopierApp, pilot: object) -> None:
    app._cfg.set_role(TID_MASTER, Role.MASTER)  # type: ignore[attr-defined]
    app._cfg.set_role(TID_SLAVE, Role.SLAVE)  # type: ignore[attr-defined]
    app._refresh()
    app.query_one("#terminals").move_cursor(row=row_of(app, TID_SLAVE))
    await pilot.pause()  # type: ignore[attr-defined]


async def test_p_pauses_a_slave_and_p_again_resumes_it(app: CopierApp, cfg: AppConfig) -> None:
    from mt5copier import config as config_mod

    async with app.run_test(size=(160, 50)) as pilot:
        await _with_slave(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].paused is True
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE, "still a slave"
        assert config_mod.load(app._saved_to).terminals[TID_SLAVE].paused is True  # type: ignore[attr-defined]
        table = app.query_one("#terminals", DataTable)
        assert "PAUSED" in str(table.get_cell(TID_SLAVE, "role"))
        assert any("paused" in t and "still closed" in t for t in _toasts(app))

        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].paused is False
        assert "SLAVE" in str(table.get_cell(TID_SLAVE, "role"))
        assert any("resumed" in t for t in _toasts(app))


async def test_pausing_asks_nothing_and_gives_up_no_copies(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await _with_slave(app, pilot)
        app._engine.copies[TID_SLAVE] = [copy_of(900)]  # type: ignore[attr-defined]
        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].paused is True
        assert not isinstance(app.screen, OrphanConfirmScreen)
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_only_a_slave_can_be_paused(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await _with_slave(app, pilot)
        app.query_one("#terminals").move_cursor(row=row_of(app, TID_MASTER))
        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_MASTER].paused is False
        assert any("Only a slave" in t for t in _toasts(app))


async def test_the_pause_cell_is_a_button_on_a_slave_row(app: CopierApp, cfg: AppConfig) -> None:
    from mt5copier.tui.app import TerminalTable as _Table

    async with app.run_test(size=(160, 50)) as pilot:
        await _with_slave(app, pilot)
        app.post_message(_Table.Clicked(TID_SLAVE, "pause"))
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].paused is True
        app.post_message(_Table.Clicked(TID_MASTER, "pause"))
        await pilot.pause()
        assert cfg.terminals[TID_MASTER].paused is False, "an empty cell does nothing"


def test_every_status_fits_its_column_uncut() -> None:
    """ "[OFF] not connected" was once cut to "not connect" by a column two cells too narrow."""
    from mt5copier.presentation import _TIER_BY_STATUS, badge_for_tier, tier_for_status
    from mt5copier.protocol import WorkerState
    from mt5copier.tui.app import _COLUMNS

    width = next(w for key, _, w in _COLUMNS if key == "status")
    for text in set(_TIER_BY_STATUS) | {str(s) for s in WorkerState}:
        badges = {badge_for_tier(tier_for_status(text))}
        if tier_for_status(text) == "good":
            badges.add(badge_for_tier("warning"))  # a held slave: connected, under [WARN]
        for badge in badges:
            assert len(f"{badge} {text}") <= width, f"{badge} {text}"
