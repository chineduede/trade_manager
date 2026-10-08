"""The UI's actions, through the keys that drive them: assigning roles, the open-copies
question, the demo-only guard, settings, pausing and panic.

The Engine is stubbed out (see ``tui_support``), so these tests spawn no processes and touch no
MT5 terminal. What they verify is that key presses produce the right *config* changes and that
the changes are persisted -- the role assignment rules are the part where a mistake would put
real trades on the wrong account.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, Static
from tui_support import (
    TID_BROKEN,
    TID_MASTER,
    TID_SLAVE,
    copy_of,
    hold,
    make_app,
    make_cfg,
    plain,
    toasts,
    wire_copying,
)

from mt5copier.config import AppConfig, NoStopPolicy, Role, SizingMode
from mt5copier.engine import TerminalView
from mt5copier.reconcile import OpenCopy
from mt5copier.store import Orphan
from mt5copier.tui import CopierApp
from mt5copier.tui.detail import DetailScreen
from mt5copier.tui.dialogs import LiveDialog, OrphanDialog
from mt5copier.tui.master import MasterForm
from mt5copier.tui.settings import Choice, SettingsForm


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return make_cfg(tmp_path)


@pytest.fixture
def app(cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CopierApp:
    return make_app(cfg, tmp_path, monkeypatch)


# --- rendering -----------------------------------------------------------------------------


async def test_every_terminal_is_listed_sorted(app: CopierApp) -> None:
    async with app.run_test():
        assert app._order == [TID_MASTER, TID_SLAVE, TID_BROKEN]


async def test_unassigned_terminals_are_not_connected(app: CopierApp, cfg: AppConfig) -> None:
    """Nothing is launched until a role is assigned -- that is the whole point of Stage 1."""
    async with app.run_test():
        assert cfg.assigned() == []


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
        await pilot.press("right")
        assert app._selected_id() == TID_SLAVE
        await pilot.press("m")
        assert cfg.terminals[TID_MASTER].role is Role.MASTER
        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED

        app._select(TID_MASTER)
        await pilot.press("u")
        app._select(TID_SLAVE)
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
        app._select(TID_BROKEN)
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


async def test_the_selection_follows_a_terminal_into_its_new_group(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        assert app._selected_id() == TID_MASTER, "it starts on a terminal"
        await pilot.press("m")
        assert app._selected_id() == TID_MASTER, "the selection followed it to the master card"
        await pilot.press("right")
        await pilot.pause()
        assert app._selected_id() == TID_SLAVE
        await pilot.press("left")
        await pilot.pause()
        assert app._selected_id() == TID_MASTER


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
        app._select(TID_SLAVE)
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
        tid = TID_MASTER
        engine.found_orphans[tid] = [Orphan(tid, 5521, "EURUSD", 0.12)]
        engine.held[tid] = "1 untracked position(s) of ours: #5521"
        await pilot.press("o")

        assert engine.acknowledged == [tid]


async def test_a_held_slave_is_announced_in_the_top_bar(app: CopierApp) -> None:
    """A hold is one slave going quiet while the others carry on, which is the easiest kind of
    silence to miss."""
    async with app.run_test() as pilot:
        engine = app._engine  # type: ignore[attr-defined]
        engine.held[TID_MASTER] = "1 untracked position(s) of ours: #5521"
        await pilot.pause()
        app._refresh()
        assert "HELD 1" in app._alerts()


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


async def _open_editor(app: CopierApp, pilot: object, terminal_id: str = TID_SLAVE) -> SettingsForm:
    """Make ``terminal_id`` a slave, select it, and open its settings.

    Needs a master to exist first -- ``role_actions.set_slave`` refuses to create a slave
    otherwise -- so this gives TID_MASTER the role directly, the same way other tests here reach
    into ``cfg``/``app._cfg`` for a precondition rather than drive it through a keypress.
    """
    app._cfg.set_role(TID_MASTER, Role.MASTER)  # type: ignore[attr-defined]
    app._select(terminal_id)
    await pilot.press("s")  # type: ignore[attr-defined]
    await pilot.press("e")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    screen = app.screen
    assert isinstance(screen, SettingsForm)
    return screen


async def test_e_opens_the_editor_for_the_selected_terminal(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        assert "Beta Broker" in str(screen.query_one("#st-title", Static).render())
        assert "slave" in str(screen.query_one("#st-where", Static).render())


async def test_the_form_starts_at_the_terminals_current_values(
    app: CopierApp, cfg: AppConfig
) -> None:
    cfg.terminals[TID_SLAVE].risk_percent = 0.35
    cfg.terminals[TID_SLAVE].max_lot = 4.0
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        assert screen.query_one("#f-risk_percent", Input).value == "0.35"
        assert screen.query_one("#f-max_lot", Input).value == "4"
        assert screen.query_one("#f-sizing_mode", Choice).value == "percent_of_balance"
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
        assert not isinstance(app.screen, SettingsForm), "a saved form closes"
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
        screen.query_one("#f-sizing_mode", Choice).value = "fixed_cash_risk"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].sizing_mode is SizingMode.FIXED_CASH_RISK


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

        assert isinstance(app.screen, SettingsForm), "a refused form stays open to be fixed"
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
        assert not isinstance(app.screen, SettingsForm)


async def test_escape_discards_the_form(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        on_disk = app._saved_to.read_bytes()  # type: ignore[attr-defined]
        screen.query_one("#f-risk_percent", Input).value = "9"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsForm)
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
        assert any("no changes" in t for t in toasts(app))


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
        assert isinstance(app.screen, SettingsForm)


async def test_the_panic_key_still_works_with_the_form_open(app: CopierApp, cfg: AppConfig) -> None:
    """Copying can be live while the form is open, and a modal hides the app's own keys."""
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        cfg.copying_enabled = True
        screen.query_one("#f-risk_percent", Input).value = "3"
        await pilot.press("f9")
        await pilot.pause()
        assert cfg.copying_enabled is False
        assert not isinstance(app.screen, SettingsForm), "the form closes after a panic"
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
        assert "DRY RUN" in str(screen.query_one("#st-mode", Static).render())
        await pilot.press("escape")
        await pilot.pause()

        cfg.dry_run = False
        await pilot.press("e")
        await pilot.pause()
        assert "LIVE" in str(app.screen.query_one("#st-mode", Static).render())


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


async def test_the_help_line_follows_focus(app: CopierApp) -> None:
    """Help for the field being edited starts with its name."""
    from mt5copier.settings_edit import BY_NAME, EDITABLE

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_editor(app, pilot)
        help_line = screen.query_one("#help", Static)
        assert str(help_line.render()).startswith(BY_NAME["risk_percent"].label)

        last = EDITABLE[-1]
        screen.query_one(f"#f-{last.name}").focus()
        await pilot.pause()
        assert str(help_line.render()).startswith(last.label)


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
    async with app.run_test() as pilot:
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        _connected_views(app, trade_mode=2)  # real money
        app._refresh()
        app._select(TID_SLAVE)
        await pilot.pause()

        assert app._kinds[TID_SLAVE] == "blocked"
        assert "allow live trading" in plain(app._cards[TID_SLAVE].content)
        assert "blocked" not in plain(app._cards[TID_MASTER].content), "masters are not held"

        cfg.terminals[TID_SLAVE].allow_live_trading = True
        app._refresh()
        assert app._kinds[TID_SLAVE] != "blocked"


async def test_a_demo_slave_is_not_blocked(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test():
        cfg.set_role(TID_MASTER, Role.MASTER)
        cfg.set_role(TID_SLAVE, Role.SLAVE)
        _connected_views(app, trade_mode=0)  # demo
        app._refresh()
        assert app._kinds[TID_SLAVE] != "blocked"


async def test_the_form_shows_the_overrides_state(app: CopierApp, cfg: AppConfig) -> None:
    cfg.terminals[TID_SLAVE].allow_live_trading = True
    async with app.run_test() as pilot:
        _connected_views(app, trade_mode=2)  # real money
        screen = await _open_editor(app, pilot)
        note = str(screen.query_one("#st-where", Static).render())
        assert "live trading allowed" in note


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
        assert screen.query_one("#f-symbol_map", Input).value == "GOLD=XAUUSDp, US30=US30.cash"
        assert screen.query_one("#f-symbol_whitelist", Input).value == "EURUSD, GBPUSD"
        assert screen.query_one("#f-symbol_blacklist", Input).value == "XAUUSD"


async def test_the_symbol_settings_are_saved(app: CopierApp, cfg: AppConfig) -> None:
    from mt5copier import config as config_mod

    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-symbol_suffix", Input).value = "p"
        screen.query_one("#f-symbol_map", Input).value = "us30 = US30.cash, nas100=USTEC"
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
        screen.query_one("#f-symbol_map", Input).value = "GOLD"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsForm)
        assert "MASTER=BROKER" in str(screen.query_one("#e-symbol_map", Static).render())
        assert cfg.terminals[TID_SLAVE].symbol_map == {}


async def test_ctrl_s_saves_from_inside_the_map(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        area = screen.query_one("#f-symbol_map", Input)
        area.focus()
        area.value = "GOLD=XAUUSDp"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].symbol_map == {"GOLD": "XAUUSDp"}


async def test_enter_in_the_map_starts_the_next_entry_and_does_not_save(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        area = screen.query_one("#f-symbol_map", Input)
        area.focus()
        area.value = "GOLD=XAUUSDp"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SettingsForm), "Enter in a list starts the next entry"
        assert area.value == "GOLD=XAUUSDp, "
        assert cfg.terminals[TID_SLAVE].symbol_map == {}


async def test_tab_leaves_the_map_instead_of_typing_a_tab(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        area = screen.query_one("#f-symbol_map", Input)
        area.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is screen.query_one("#f-symbol_whitelist", Input)
        assert "\t" not in area.value


async def test_escape_closes_the_form_from_inside_the_map(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-symbol_map", Input).focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsForm)


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


def _text(screen: object, selector: str) -> str:
    return plain(screen.query_one(selector, Static).content)  # type: ignore[attr-defined]


def _go(question: OrphanDialog) -> str:
    """What the button that agrees says."""
    return str(question.query_one("#go", Button).label)


async def _save_with_copies(
    app: CopierApp, pilot: object, *copies: OpenCopy
) -> tuple[SettingsForm, OrphanDialog]:
    """Open the form, change a setting, save it, and return both screens: the form, and the
    question it provoked."""
    form = await _open_editor(app, pilot)
    hold(app, *copies)
    form.query_one("#f-risk_percent", Input).value = "0.3"
    await pilot.press("ctrl+s")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    question = app.screen
    assert isinstance(question, OrphanDialog)
    return form, question


async def test_a_slave_with_no_copies_saves_without_being_asked(
    app: CopierApp, cfg: AppConfig
) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-invert", Choice).value = "opposite"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].invert is True
        assert not isinstance(app.screen, SettingsForm | OrphanDialog)


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
        assert "CHANGE BETA BROKER'S SETTINGS?" in _dialog_text(question)
        assert "orphan 2 copies" in _go(question)
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
        assert isinstance(app.screen, SettingsForm)
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

        assert isinstance(app.screen, SettingsForm), "the form is still open"
        assert form.query_one("#f-risk_percent", Input).value == "0.3", "with the edit still typed"
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0
        assert app._engine.given_up == []  # type: ignore[attr-defined]
        assert app._engine.copies[TID_SLAVE], "the copies are still tracked"  # type: ignore[attr-defined]
        assert app._saved_to.read_bytes() == on_disk  # type: ignore[attr-defined]
        assert not any("orphan" in t for t in toasts(app))


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
        assert not isinstance(app.screen, SettingsForm | OrphanDialog), "both close"
        reloaded = config_mod.load(app._saved_to)  # type: ignore[attr-defined]
        assert reloaded.terminals[TID_SLAVE].risk_percent == 0.3
        (said,) = [t for t in toasts(app) if "orphaned" in t]
        assert "2 open copies" in said and "#900" in said and "#901" in said
        assert "by hand" in said, "and what it means for them"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("f-risk_percent", "0.3"),
        ("f-max_lot", "2"),
        ("f-symbol_suffix", "p"),
        ("f-invert", "opposite"),
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
        assert isinstance(app.screen, OrphanDialog), field


async def test_saving_a_form_that_changed_nothing_asks_nothing(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        await _open_editor(app, pilot)
        hold(app, copy_of(11))
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, OrphanDialog)
        assert any("no changes" in t for t in toasts(app))


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

        assert isinstance(app.screen, SettingsForm)
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

        assert isinstance(app.screen, SettingsForm)
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

        assert isinstance(app.screen, SettingsForm)
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
        assert not isinstance(app.screen, OrphanDialog)
        assert cfg.terminals[TID_SLAVE].risk_percent == 1.0, "and it agreed to nothing"
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_a_dry_run_says_the_copies_are_simulated(app: CopierApp, cfg: AppConfig) -> None:
    cfg.dry_run = True
    async with app.run_test(size=(100, 40)) as pilot:
        _, question = await _save_with_copies(app, pilot, copy_of(11))
        assert "simulated" in _dialog_text(question)
        question.query_one("#go", Button).press()
        await pilot.pause()
        (said,) = [t for t in toasts(app) if "simulated" in t]
        assert "by hand" not in said, "there is nothing real to close"


async def test_a_live_session_does_not_call_the_copies_simulated(
    app: CopierApp, cfg: AppConfig
) -> None:
    cfg.dry_run = False
    async with app.run_test(size=(100, 40)) as pilot:
        _, question = await _save_with_copies(app, pilot, copy_of(11))
        assert "simulated" not in _dialog_text(question)


async def test_a_long_list_is_cut_but_the_count_is_whole(app: CopierApp) -> None:
    async with app.run_test(size=(100, 60)) as pilot:
        _, question = await _save_with_copies(app, pilot, *(copy_of(i) for i in range(20, 32)))
        assert "orphan 12 copies" in _go(question)
        listing = _text(question, "#copies")
        assert "#1027" in listing and "#1028" not in listing
        assert "and 4 more" in _dialog_text(question)


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
        assert "Apply and orphan 4 copies" in drawn


# --- warnings and the record ---------------------------------------------------------------


async def test_enabling_no_stop_copying_is_flagged(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test() as pilot:
        screen = await _open_editor(app, pilot)
        screen.query_one("#f-no_stop_policy", Choice).value = "lot_multiple"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].no_stop_policy is NoStopPolicy.LOT_MULTIPLE
        assert any("UNPROTECTED" in t for t in toasts(app))


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
) -> LiveDialog:
    """Select ``terminal_id``, give it a role with ``role_key`` (None keeps its own), press l."""
    if role_key == "s":
        # Same precondition _open_editor needs: a slave can't be created with no master.
        app._cfg.set_role(TID_MASTER, Role.MASTER)  # type: ignore[attr-defined]
    app._select(terminal_id)
    if role_key is not None:
        await pilot.press(role_key)  # type: ignore[attr-defined]
    await pilot.press("l")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    screen = app.screen
    assert isinstance(screen, LiveDialog)
    return screen


def _alerts(app: CopierApp) -> str:
    """What the top bar shouts about."""
    return "  ".join(app._alerts())


def _dialog_text(screen: object) -> str:
    """Everything a dialog says, as plain text."""
    return " ".join(plain(w.content) for w in screen.query(Static))  # type: ignore[attr-defined]


async def test_l_opens_the_override_for_the_selected_terminal(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        title = str(screen.query_one(".dialog-title", Static).render())
        assert "ALLOW LIVE TRADING ON BETA BROKER" in title, "it is off, so it offers on"
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
        assert not isinstance(app.screen, LiveDialog), "a confirmed dialog closes"
        assert app._engine.sync_calls > syncs  # type: ignore[attr-defined]
        reloaded = config_mod.load(app._saved_to)  # type: ignore[attr-defined]
        assert reloaded.terminals[TID_SLAVE].allow_live_trading is True
        assert any("prop" in t for t in toasts(app)), "lifting the guard is said out loud"


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

        assert isinstance(app.screen, LiveDialog), "the dialog stays open"
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
        assert isinstance(app.screen, LiveDialog)
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
        assert isinstance(app.screen, LiveDialog)
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
        assert not isinstance(app.screen, LiveDialog)
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
        assert isinstance(app.screen, LiveDialog)


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
        assert not isinstance(app.screen, LiveDialog)
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False, "what was typed is dropped"


async def test_turning_it_off_takes_one_press_and_no_typing(app: CopierApp, cfg: AppConfig) -> None:
    """The safe direction. There is no box to type in, and Enter on the button does it."""
    cfg.set_role(TID_SLAVE, Role.SLAVE)
    cfg.terminals[TID_SLAVE].allow_live_trading = True
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot, role_key=None)
        assert not screen.query("#typed"), "nothing to type"
        assert "TURN OFF LIVE TRADING" in str(screen.query_one(".dialog-title", Static).render())
        assert app.focused is screen.query_one("#go")
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False
        assert not isinstance(app.screen, LiveDialog)


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
        assert isinstance(question, OrphanDialog)
        assert "allow_live_trading off -> on" in _text(question, "#changes")
        assert cfg.terminals[TID_SLAVE].allow_live_trading is False, "not yet"

        question.query_one("#go", Button).press()
        await pilot.pause()

        assert cfg.terminals[TID_SLAVE].allow_live_trading is True
        assert app._engine.given_up == [(TID_SLAVE, [11])]  # type: ignore[attr-defined]
        assert not isinstance(app.screen, LiveDialog | OrphanDialog)


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

        assert isinstance(app.screen, LiveDialog)
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
        assert isinstance(question, OrphanDialog)
        assert "orphan 2 copies" in _go(question)

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
        assert not isinstance(app.screen, OrphanDialog)
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


async def test_the_top_bar_says_which_terminals_have_the_guard_lifted(
    app: CopierApp, cfg: AppConfig
) -> None:
    """Said on the main screen, not only in the dialog. It also covers a terminal that is not a
    slave, because the flag survives a role change and would otherwise be invisible."""
    async with app.run_test():
        app._refresh()
        assert "NON-DEMO" not in _alerts(app)

        cfg.terminals[TID_SLAVE].allow_live_trading = True
        app._refresh()
        assert "NON-DEMO ALLOWED: Beta Broker" in _alerts(app)

        cfg.terminals[TID_MASTER].allow_live_trading = True
        app._refresh()
        text = _alerts(app)
        assert "Beta Broker" in text and "Alpha Broker" in text

        cfg.terminals[TID_SLAVE].allow_live_trading = False
        cfg.terminals[TID_MASTER].allow_live_trading = False
        app._refresh()
        assert "NON-DEMO" not in _alerts(app)


async def test_the_top_bar_follows_the_dialog(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        screen = await _open_override(app, pilot)
        screen.query_one("#typed", Input).value = "Beta Broker"
        await pilot.press("enter")
        await pilot.pause()
        app._refresh()
        assert "NON-DEMO ALLOWED: Beta Broker" in _alerts(app)

        await pilot.press("l")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app._refresh()
        assert "NON-DEMO" not in _alerts(app)


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
        assert "Allow live trading" in drawn


# --- unassigning over open copies asks first --------------------------------------------------


async def _press_unassign_on(app: CopierApp, pilot: object, terminal_id: str) -> None:
    app._select(terminal_id)
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
        assert isinstance(question, OrphanDialog)
        assert "UNASSIGN BETA BROKER?" in _dialog_text(question)
        assert "#900" in plain(question.query_one("#copies", Static).content)
        assert "It holds open copies" in _dialog_text(question)
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

        def told() -> bool:
            return any("#900" in t and "close them by hand" in t for t in toasts(app))

        # The toast follows the dialog's dismissal by a message or two; one pause was not
        # always enough on a busy machine.
        for _ in range(20):
            await pilot.pause(0.05)
            if told():
                break

        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED
        assert app._engine.given_up == [(TID_SLAVE, [11])]  # type: ignore[attr-defined]
        assert told()


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
        assert isinstance(question, OrphanDialog)
        copies = plain(question.query_one("#copies", Static).content)
        assert "Beta Broker" in copies, "named by the slave holding them"
        assert "slave is released too" in _dialog_text(question), "and it says the slave goes"

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
        assert not isinstance(app.screen, OrphanDialog)
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED


# --- the terminal detail page ----------------------------------------------------------------


def _detail_text(app: CopierApp, widget_id: str) -> str:
    return plain(app.screen.query_one(widget_id, Static).content)


async def _open(app: CopierApp, pilot: object, terminal_id: str) -> DetailScreen:
    app._refresh()
    app._select(terminal_id)
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    assert isinstance(app.screen, DetailScreen)
    return app.screen


async def test_enter_opens_the_terminal_page_and_escape_returns(
    app: CopierApp, cfg: AppConfig
) -> None:
    wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await _open(app, pilot, TID_SLAVE)
        assert screen.terminal_id == TID_SLAVE
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0], "back on the map"


async def test_the_slave_page_shows_positions_risk_and_health(
    app: CopierApp, cfg: AppConfig
) -> None:
    wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await _open(app, pilot, TID_SLAVE)
        band = _detail_text(app, "#td-band")
        assert "Beta Broker" in band
        assert "demo" in band
        assert "41 ms" in band
        assert "40.00" in band

        account = _detail_text(app, "#td-account")
        assert "10%" in account, "500 of 5,040 equity is in use"
        assert "1,008%" in account
        held = _detail_text(app, "#td-positions-head")
        assert "EURUSD" in held and "GBPUSD" in held

        copying = _detail_text(app, "#td-copying")
        assert "95 / 95 / 120 ms" in copying
        assert "4 pts worse" in copying
        assert "1 / 0 / 0" in copying, "one refused"

        table = screen.query_one("#td-positions")
        assert table.row_count == 3, "two positions and their total"  # type: ignore[attr-defined]
        links = [str(table.get_row_at(i)[-1]) for i in range(2)]  # type: ignore[attr-defined]
        assert links == ["#100 (master 1 lots)", "manual / not ours"]


def _exempt(app: CopierApp) -> bool:
    (copy,) = app._engine.open_copies(TID_SLAVE) or ()  # type: ignore[attr-defined]
    return copy.breakeven_exempt


async def test_b_exempts_the_selected_copy_and_b_again_lifts_it(
    app: CopierApp, cfg: AppConfig
) -> None:
    wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        await _open(app, pilot, TID_SLAVE)
        assert not _exempt(app)
        await pilot.press("b")
        await pilot.pause()
        assert _exempt(app)
        await pilot.press("b")
        await pilot.pause()
        assert not _exempt(app)


async def test_b_on_a_position_that_is_not_a_copy_does_nothing(
    app: CopierApp, cfg: AppConfig
) -> None:
    wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await _open(app, pilot, TID_SLAVE)
        screen.query_one("#td-positions", DataTable).move_cursor(row=1)
        await pilot.press("b")
        await pilot.pause()
        assert not any(c.breakeven_exempt for c in app._engine.open_copies(TID_SLAVE) or ())  # type: ignore[attr-defined]


async def test_the_master_page_counts_slaves_and_has_no_copy_health(
    app: CopierApp, cfg: AppConfig
) -> None:
    wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await _open(app, pilot, TID_MASTER)
        table = screen.query_one("#td-positions")
        assert str(table.get_row_at(0)[-1]) == "on 1/1 slaves"  # type: ignore[attr-defined]
        assert "no copying to time" in _detail_text(app, "#td-copying")


async def test_a_terminal_with_no_snapshot_opens_without_crashing(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await _open(app, pilot, TID_BROKEN)
        assert "not connected yet" in _detail_text(app, "#td-account")


async def test_e_on_the_terminal_page_opens_that_terminals_settings_and_esc_comes_back(
    app: CopierApp, cfg: AppConfig
) -> None:
    wire_copying(app, cfg)
    async with app.run_test(size=(160, 50)) as pilot:
        await _open(app, pilot, TID_SLAVE)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, SettingsForm)
        assert app.screen._current().terminal_id == TID_SLAVE  # type: ignore[attr-defined]
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen), "closing the form returns to the page"


async def test_the_master_form_sets_only_its_symbol_decoration(
    app: CopierApp, cfg: AppConfig
) -> None:
    """e on the master opens its own small form, not a slave's, and saving it keeps the copies."""
    async with app.run_test(size=(160, 50)) as pilot:
        app._cfg.set_role(TID_MASTER, Role.MASTER)  # type: ignore[attr-defined]
        app._refresh()
        app._select(TID_MASTER)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, MasterForm)
        assert not app.screen.query("#f-risk_percent")
        app.screen.query_one("#f-symbol_suffix", Input).value = "p"
        await pilot.pause()
        assert "EURUSDp → EURUSD" in str(app.screen.query_one("#ex-body", Static).render())
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, MasterForm)
        assert cfg.terminals[TID_MASTER].symbol_suffix == "p"


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
    app._select(TID_SLAVE)
    await pilot.pause()  # type: ignore[attr-defined]


async def test_p_pauses_a_slave_and_p_again_resumes_it(app: CopierApp, cfg: AppConfig) -> None:
    from mt5copier import config as config_mod

    async with app.run_test(size=(160, 50)) as pilot:
        _connected_views(app, trade_mode=0)  # a link that is down would outrank the pause
        await _with_slave(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].paused is True
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE, "still a slave"
        assert config_mod.load(app._saved_to).terminals[TID_SLAVE].paused is True  # type: ignore[attr-defined]
        assert app._kinds[TID_SLAVE] == "paused"
        assert any("paused" in t and "still closed" in t for t in toasts(app))

        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].paused is False
        assert app._kinds[TID_SLAVE] != "paused"
        assert any("resumed" in t for t in toasts(app))


async def test_pausing_asks_nothing_and_gives_up_no_copies(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await _with_slave(app, pilot)
        app._engine.copies[TID_SLAVE] = [copy_of(900)]  # type: ignore[attr-defined]
        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].paused is True
        assert not isinstance(app.screen, OrphanDialog)
        assert app._engine.given_up == []  # type: ignore[attr-defined]


async def test_only_a_slave_can_be_paused(app: CopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await _with_slave(app, pilot)
        app._select(TID_MASTER)
        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_MASTER].paused is False
        assert any("Only a slave" in t for t in toasts(app))
