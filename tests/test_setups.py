"""Saved setups: what is kept, what loading one gives up, and the page that does both."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_reconcile import TID_SLAVE2, held_by, live_engine, seed_live_store, stale_row
from textual.widgets import Button, DataTable
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
)

from mt5copier import setups as setups_mod
from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.setups import Setup, SetupsDoc
from mt5copier.tui.dialogs import OrphanDialog
from mt5copier.tui.guards import GuardConfirm
from mt5copier.tui.setups import SetupsScreen
from mt5copier.tui.widgets import TopBar

TID_THIRD = "D" * 32


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    """Alpha is the master and Beta its slave; Delta is a third terminal, unassigned."""
    c = make_cfg(tmp_path)
    exe = c.terminals[TID_MASTER].exe_path
    c.terminals[TID_THIRD] = TerminalConfig(
        terminal_id=TID_THIRD, label="Delta Broker", exe_path=exe, last_login=444
    )
    c.terminals[TID_MASTER].role = Role.MASTER
    c.terminals[TID_SLAVE].role = Role.SLAVE
    return c


def roles(cfg: AppConfig) -> dict[str, Role]:
    return {tid: t.role for tid, t in cfg.terminals.items()}


# --- the file and what it answers -------------------------------------------------------------


def test_capture_takes_the_roles_and_whether_copying_is_on(cfg: AppConfig) -> None:
    cfg.copying_enabled = True
    setup = setups_mod.capture(cfg)
    assert setup is not None
    assert (setup.master, setup.slaves, setup.copying) == (TID_MASTER, (TID_SLAVE,), True)


def test_there_is_nothing_to_capture_with_no_master(cfg: AppConfig) -> None:
    cfg.terminals[TID_MASTER].role = Role.UNASSIGNED
    cfg.terminals[TID_SLAVE].role = Role.UNASSIGNED
    assert setups_mod.capture(cfg) is None


def test_the_current_setup_is_the_one_with_the_same_roles(cfg: AppConfig) -> None:
    doc = SetupsDoc(
        setups={
            "other": Setup(master=TID_THIRD, slaves=(TID_SLAVE,)),
            "main": Setup(master=TID_MASTER, slaves=(TID_SLAVE,), copying=True),
        }
    )
    assert setups_mod.current_name(doc, cfg) == "main", "copying on or off makes no difference"
    cfg.set_role(TID_THIRD, Role.SLAVE)
    assert setups_mod.current_name(doc, cfg) is None


def test_loading_another_master_puts_every_role_in_place(cfg: AppConfig) -> None:
    setups_mod.apply_roles(Setup(master=TID_THIRD, slaves=(TID_MASTER,), copying=True), cfg)
    assert roles(cfg) == {
        TID_MASTER: Role.SLAVE,
        TID_SLAVE: Role.UNASSIGNED,
        TID_BROKEN: Role.UNASSIGNED,
        TID_THIRD: Role.MASTER,
    }
    assert cfg.copying_enabled


def test_a_slave_that_stays_one_stays_paused(cfg: AppConfig) -> None:
    cfg.terminals[TID_SLAVE].paused = True
    setups_mod.apply_roles(Setup(master=TID_MASTER, slaves=(TID_SLAVE, TID_THIRD)), cfg)
    assert cfg.terminals[TID_SLAVE].paused
    assert cfg.terminals[TID_THIRD].role is Role.SLAVE


def test_who_loses_their_copies(cfg: AppConfig) -> None:
    same_master = Setup(master=TID_MASTER, slaves=(TID_THIRD,))
    assert setups_mod.losing(same_master, cfg) == [TID_SLAVE], "taken off its master"
    kept = Setup(master=TID_MASTER, slaves=(TID_SLAVE, TID_THIRD))
    assert setups_mod.losing(kept, cfg) == [], "same master, still a slave: copies stay"
    new_master = Setup(master=TID_THIRD, slaves=(TID_SLAVE,))
    assert setups_mod.losing(new_master, cfg) == [TID_SLAVE], "what it copied was the old one's"


def test_a_setup_naming_a_terminal_that_cannot_connect_cannot_be_loaded(cfg: AppConfig) -> None:
    setup = Setup(master=TID_MASTER, slaves=(TID_BROKEN, "E" * 32))
    assert setups_mod.problems(setup, cfg) == [
        "Gone Broker has no terminal executable",
        "terminal EEEEEEEE is no longer found",
    ]


def test_the_file_round_trips_and_a_corrupt_one_is_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "setups.json"
    doc = SetupsDoc(setups={"main": Setup(master=TID_MASTER, slaves=(TID_SLAVE,), saved_at=1.0)})
    setups_mod.save(doc, path)
    assert setups_mod.load(path) == doc

    path.write_text("{not json", encoding="utf-8")
    assert setups_mod.load(path) == SetupsDoc()
    assert not path.exists()
    assert list(tmp_path.glob("setups.bad-*.json"))


# --- the engine gives up copies only as agreed --------------------------------------------------


@pytest.fixture
def engine_cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        terminals={
            tid: TerminalConfig(terminal_id=tid, label=label, exe_path=exe, role=role)
            for tid, label, role in (
                (TID_MASTER, "Master", Role.MASTER),
                (TID_SLAVE, "Slave", Role.SLAVE),
                (TID_SLAVE2, "Other", Role.UNASSIGNED),
            )
        },
    )


def test_a_new_master_is_refused_until_the_slaves_copies_are_agreed_to(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    setup = Setup(master=TID_SLAVE2, slaves=(TID_SLAVE,), copying=True)

    stake = eng.setup_stake(setup)
    assert stake is not None and [c.master_ticket for c in stake[TID_SLAVE]] == [11]
    refusal = eng.load_setup(setup)
    assert refusal is not None and "confirmed" in refusal
    assert eng.config.terminals[TID_MASTER].role is Role.MASTER, "nothing changed"
    assert not eng.config.copying_enabled

    assert eng.load_setup(setup, orphan={TID_SLAVE: [11]}) is None
    assert eng.config.terminals[TID_SLAVE2].role is Role.MASTER
    assert eng.config.terminals[TID_MASTER].role is Role.UNASSIGNED
    assert eng.config.copying_enabled
    assert held_by(eng) == [], "no record of the old master's tickets is left to act on"
    assert [o.slave_ticket for o in eng.orphans(TID_SLAVE)] == [900]


def test_a_slave_kept_under_the_same_master_keeps_its_copies(
    engine_cfg: AppConfig, tmp_path: Path
) -> None:
    seed_live_store(tmp_path, stale_row())
    eng = live_engine(engine_cfg, tmp_path)
    setup = Setup(master=TID_MASTER, slaves=(TID_SLAVE, TID_SLAVE2))

    assert eng.setup_stake(setup) == {}
    assert eng.load_setup(setup) is None
    assert eng.config.terminals[TID_SLAVE2].role is Role.SLAVE
    assert held_by(eng) == [11], "still managed"


# --- the page ----------------------------------------------------------------------------------


async def test_w_opens_the_page_and_a_saves_the_roles_in_place_now(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(cfg, tmp_path, monkeypatch)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("w")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SetupsScreen)
        await pilot.press("a", *"prop week", "enter")
        await pilot.pause()

        assert set(app._setups.setups) == {"prop week"}
        assert setups_mod.load(tmp_path / "setups.json") == app._setups, "written to the file"
        table = screen.query_one("#st-table", DataTable)
        assert table.row_count == 1
        assert "● prop week" in plain(table.get_row_at(0)[0])
        assert "Setup prop week" in plain(screen.query_one("#topbar", TopBar).content)

        await pilot.press("a", *"prop week", "enter")
        await pilot.pause()
        assert "already saved" in plain(screen.query_one("#st-ask-error").render())


async def test_loading_a_setup_asks_then_puts_its_roles_in_place(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(cfg, tmp_path, monkeypatch)
    app._setups.setups["delta"] = Setup(master=TID_THIRD, slaves=(TID_MASTER,))
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("w", "enter")
        await pilot.pause()
        question = app.screen
        assert isinstance(question, GuardConfirm)
        question.query_one("#go", Button).press()
        await pilot.pause()

        assert cfg.terminals[TID_THIRD].role is Role.MASTER
        assert cfg.terminals[TID_MASTER].role is Role.SLAVE
        assert cfg.terminals[TID_SLAVE].role is Role.UNASSIGNED
        assert any("setup delta loaded" in t for t in toasts(app))
        assert app._engine.sync_calls  # type: ignore[attr-defined]


async def test_loading_a_setup_that_strands_copies_shows_them_first(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(cfg, tmp_path, monkeypatch)
    app._setups.setups["delta"] = Setup(master=TID_THIRD, slaves=(TID_SLAVE,))
    hold(app, copy_of(100))
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("w", "enter")
        await pilot.pause()
        question = app.screen
        assert isinstance(question, OrphanDialog)
        assert "Load and orphan 1 copy" in str(question.query_one("#go", Button).label)

        await pilot.press("escape")
        await pilot.pause()
        assert cfg.terminals[TID_MASTER].role is Role.MASTER, "kept: nothing changed"

        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#go", Button).press()
        await pilot.pause()
        assert cfg.terminals[TID_THIRD].role is Role.MASTER
        assert app._engine.given_up == [(TID_SLAVE, [100])]  # type: ignore[attr-defined]


async def test_a_setup_that_cannot_load_says_why_and_changes_nothing(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(cfg, tmp_path, monkeypatch)
    app._setups.setups["broken"] = Setup(master=TID_BROKEN)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("w", "enter")
        await pilot.pause()
        assert isinstance(app.screen, SetupsScreen)
        assert any("no terminal executable" in t for t in toasts(app))
        assert cfg.terminals[TID_MASTER].role is Role.MASTER


async def test_rename_and_delete(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(cfg, tmp_path, monkeypatch)
    app._setups.setups["old"] = Setup(master=TID_MASTER, slaves=(TID_SLAVE,))
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("w", "n", *["backspace"] * 3, *"new", "enter")
        await pilot.pause()
        assert set(app._setups.setups) == {"new"}

        await pilot.press("x")
        await pilot.pause()
        app.screen.query_one("#go", Button).press()
        await pilot.pause()
        assert app._setups.setups == {}
        assert setups_mod.load(tmp_path / "setups.json") == SetupsDoc()
        assert cfg.terminals[TID_MASTER].role is Role.MASTER, "the roles in place stay"
