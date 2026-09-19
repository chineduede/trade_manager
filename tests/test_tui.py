"""UI tests via Textual's headless harness.

The Engine is stubbed out, so these tests spawn no processes and touch no MT5 terminal. What
they verify is that key presses and clicks produce the right *config* changes and that the
changes are persisted -- the role assignment rules are the part where a mistake would put real
trades on the wrong account.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.engine import RecordedAction, TerminalView
from mt5copier.store import Orphan
from mt5copier.tui.app import CopierApp

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
