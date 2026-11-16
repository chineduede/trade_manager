"""The shared stores: the one-time migration, the overlays, and the merges that let two
profile windows write one file without erasing each other's side of it."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from mt5copier import config as config_mod
from mt5copier import settings_edit, shared_data
from mt5copier.config import AppConfig, TerminalConfig
from mt5copier.guards import AccountGuards, GuardBook, GuardRule, GuardsDoc
from mt5copier.guards import model as guards_model
from mt5copier.paths import (
    account_names_path,
    base_data_dir,
    guards_path,
    machine_path,
    news_path,
    profiles_root,
    shared_dir,
)


@pytest.fixture(autouse=True)
def _fresh_stamp() -> None:
    """The names-mtime cache is module state; a stamp left by one test's file could, by
    coincidence of the clock, match another test's and make a refresh read as no change."""
    shared_data._names_stamp_seen = None


def _write(path: Path, payload: object, *, age: float = 0.0) -> None:
    """A profile file as migration finds it, optionally backdated so mtime order is fixed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    if age:
        then = time.time() - age
        os.utime(path, (then, then))


# --- migration ---------------------------------------------------------------------------------


def test_migrate_gathers_every_profile_later_wins() -> None:
    old, new = base_data_dir(), profiles_root() / "beta"
    _write(
        old / "config.json",
        {"account_names": {"1@A": "old-name", "2@B": "only-old"}, "poll_interval_ms": 100},
        age=60,
    )
    _write(
        new / "config.json",
        {"account_names": {"1@A": "new-name"}, "poll_interval_ms": 250},
    )
    _write(old / "guards.json", {"accounts": {"1@A": {"rules": []}}, "templates": {}}, age=60)
    _write(
        new / "guards.json",
        {"accounts": {"2@B": {"rules": []}}, "templates": {"t": []}},
    )
    _write(old / "guard_state.json", {"day_equity": {"1@A": {"r": {"day": "d"}}}}, age=60)
    _write(new / "news.json", {"fetched": 1})

    shared_data.migrate()

    names = json.loads(account_names_path().read_text(encoding="utf-8"))
    assert names == {"1@A": "new-name", "2@B": "only-old"}
    machine = json.loads(machine_path().read_text(encoding="utf-8"))
    assert machine["poll_interval_ms"] == 250
    guards = json.loads(guards_path().read_text(encoding="utf-8"))
    assert set(guards["accounts"]) == {"1@A", "2@B"}
    assert guards["templates"] == {"t": []}
    assert json.loads((shared_dir() / "guard_state.json").read_text(encoding="utf-8"))[
        "day_equity"
    ] == {"1@A": {"r": {"day": "d"}}}
    assert json.loads(news_path().read_text(encoding="utf-8")) == {"fetched": 1}


def test_migrate_runs_once() -> None:
    _write(base_data_dir() / "config.json", {"account_names": {"1@A": "first"}})
    shared_data.migrate()
    _write(base_data_dir() / "config.json", {"account_names": {"1@A": "second"}})
    shared_data.migrate()
    names = json.loads(account_names_path().read_text(encoding="utf-8"))
    assert names == {"1@A": "first"}


def test_migrate_with_nothing_still_marks() -> None:
    shared_data.migrate()
    assert (shared_dir() / "migrated.json").exists()
    assert not account_names_path().exists()


# --- load overlays -----------------------------------------------------------------------------


def test_load_overlays_shared_names_and_timings() -> None:
    _write(account_names_path(), {"1@A": "Alpha"})
    _write(machine_path(), {"poll_interval_ms": 777, "rescan_interval_s": 60})
    cfg = config_mod.load()
    assert cfg.account_names == {"1@A": "Alpha"}
    assert cfg.poll_interval_ms == 777
    assert cfg.rescan_interval_s == 60


def test_bad_shared_timings_keep_the_profiles_own() -> None:
    _write(machine_path(), {"poll_interval_ms": -5})
    cfg = config_mod.load()
    assert cfg.poll_interval_ms == AppConfig().poll_interval_ms


def test_shared_names_win_over_the_profiles_copy() -> None:
    cfg = AppConfig()
    cfg.account_names["1@A"] = "stale"
    config_mod.save(cfg)
    _write(account_names_path(), {"1@A": "fresh"})
    assert config_mod.load().account_names == {"1@A": "fresh"}


# --- renames -----------------------------------------------------------------------------------


def _cfg_with_account() -> AppConfig:
    cfg = AppConfig()
    cfg.terminals["t1"] = TerminalConfig(terminal_id="t1", last_login=1001, last_server="Prop-Demo")
    return cfg


def test_rename_writes_and_clears_the_shared_store() -> None:
    cfg = _cfg_with_account()
    assert settings_edit.rename(cfg, "t1", "Alpha") is None
    assert json.loads(account_names_path().read_text(encoding="utf-8")) == {
        "1001@Prop-Demo": "Alpha"
    }
    assert settings_edit.rename(cfg, "t1", "") is None
    assert json.loads(account_names_path().read_text(encoding="utf-8")) == {}


def test_rename_keeps_another_windows_entry() -> None:
    shared_data.set_name("9@Elsewhere", "Theirs")
    cfg = _cfg_with_account()
    settings_edit.rename(cfg, "t1", "Mine")
    names = json.loads(account_names_path().read_text(encoding="utf-8"))
    assert names == {"9@Elsewhere": "Theirs", "1001@Prop-Demo": "Mine"}


def test_refresh_names_picks_up_another_windows_rename() -> None:
    cfg = AppConfig()
    assert shared_data.refresh_names(cfg) is False
    _write(account_names_path(), {"1@A": "FromElsewhere"})
    then = time.time() + 2  # a plainly different mtime, whatever the clock's resolution
    os.utime(account_names_path(), (then, then))
    assert shared_data.refresh_names(cfg) is True
    assert cfg.account_names == {"1@A": "FromElsewhere"}
    assert shared_data.refresh_names(cfg) is False


# --- guards: two windows, one file -------------------------------------------------------------


def test_guards_save_folds_in_the_other_windows_accounts() -> None:
    rule = GuardRule(type="daily_loss")
    first = GuardsDoc(accounts={"1@A": AccountGuards(rules=(rule,))})
    guards_model.save(first)
    second = GuardsDoc(accounts={"2@B": AccountGuards()}, templates={"mine": (rule,)})
    guards_model.save(second)
    on_disk = guards_model.load()
    assert set(on_disk.accounts) == {"1@A", "2@B"}
    assert on_disk.accounts["1@A"].rules[0].type == "daily_loss"
    assert set(second.accounts) == {"1@A", "2@B"}  # the doc adopted what disk held


def test_guards_save_memory_wins_per_account() -> None:
    key = "1@A"
    guards_model.save(GuardsDoc(accounts={key: AccountGuards(initial_balance=50_000.0)}))
    mine = GuardsDoc(accounts={key: AccountGuards(initial_balance=100_000.0)})
    guards_model.save(mine)
    assert guards_model.load().accounts[key].initial_balance == 100_000.0


def test_guard_state_save_merges_per_dirty_account() -> None:
    path = shared_dir() / "guard_state.json"
    _write(path, {"day_equity": {"theirs@X": {"r": {"day": "d", "equity": 1.0}}}, "latched": {}})
    book = GuardBook(GuardsDoc(), state_path=path)
    book._day_equity["mine@Y"] = {"r2": {"day": "d", "equity": 2.0}}
    book._dirty.add("mine@Y")
    book._save()
    state = json.loads(path.read_text(encoding="utf-8"))
    assert set(state["day_equity"]) == {"theirs@X", "mine@Y"}


def test_guard_book_adopts_an_account_from_disk() -> None:
    path = shared_dir() / "guard_state.json"
    book = GuardBook(GuardsDoc(), state_path=path)
    _write(
        path,
        {"day_equity": {}, "latched": {"handed@X": {"r": {"until": "hand", "at": 1.0}}}},
    )
    book._adopt("handed@X")
    assert "handed@X" in book._latched
    # But never over this window's own changes: a dirty key is ours.
    book._latched["handed@X"] = {}
    book._dirty.add("handed@X")
    book._adopt("handed@X")
    assert book._latched["handed@X"] == {}
