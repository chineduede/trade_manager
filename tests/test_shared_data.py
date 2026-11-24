"""The shared stores: the one-time migrations (profiles into the shared folder, its JSON
into the database), the overlays, and the merges that let two profile windows write one
store without erasing each other's side of it."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from mt5copier import config as config_mod
from mt5copier import settings_edit, shared_data, shared_db
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
    """The names-stamp cache is module state; a stamp left by one test could otherwise
    carry into another and make a refresh read as no change."""
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


def test_the_database_imports_the_shared_json_once() -> None:
    _write(account_names_path(), {"1@A": "Alpha"})
    _write(
        guards_path(), {"version": 1, "accounts": {"1@A": {"rules": []}}, "templates": {"t": []}}
    )
    _write(
        shared_dir() / "guard_state.json",
        {
            "day_equity": {"1@A": {"r": {"day": "d"}}},
            "opens": {"1@A": {"day": "d", "market": 2, "pending": 0}},
        },
    )
    _write(
        shared_dir() / "prop_accounts.json",
        {"version": 1, "containers": {"abc12345": {"name": "FTMO", "stages": ["1@A"]}}},
    )

    assert shared_data.load_names() == {"1@A": "Alpha"}
    doc = guards_model.load()
    assert "1@A" in doc.accounts and set(doc.templates) == {"t"}
    db = shared_db.get()
    assert db.fetch("guard_state", "1@A") == {
        "day_equity": {"r": {"day": "d"}},
        "opens": {"day": "d", "market": 2, "pending": 0},
    }
    assert db.fetch("prop_containers", "abc12345") == {"name": "FTMO", "stages": ["1@A"]}
    # Once only: the files are the rollback story, not a live source -- an edit to one
    # after the import changes nothing.
    _write(account_names_path(), {"1@A": "Changed"})
    assert shared_data.load_names() == {"1@A": "Alpha"}


def test_a_fresh_database_with_no_json_starts_empty() -> None:
    assert shared_data.load_names() is None  # never written: the profile's own copy counts
    assert guards_model.load().accounts == {}
    assert shared_db.get().load("prop_containers") == {}


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
    assert shared_data.load_names() == {"1001@Prop-Demo": "Alpha"}
    assert settings_edit.rename(cfg, "t1", "") is None
    assert shared_data.load_names() == {}


def test_rename_keeps_another_windows_entry() -> None:
    shared_data.set_name("9@Elsewhere", "Theirs")
    cfg = _cfg_with_account()
    settings_edit.rename(cfg, "t1", "Mine")
    assert shared_data.load_names() == {"9@Elsewhere": "Theirs", "1001@Prop-Demo": "Mine"}


def test_refresh_names_picks_up_another_windows_rename() -> None:
    cfg = AppConfig()
    assert shared_data.refresh_names(cfg) is False
    # Another window's write: straight into the store, not through set_name, which would
    # also update this window's seen-stamp and so hide its own change.
    db = shared_db.get()
    with db.change(shared_db.NAMES):
        db.put("account_names", "1@A", "FromElsewhere")
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


def test_delete_template_holds_through_a_later_save() -> None:
    rule = GuardRule(type="daily_loss")
    guards_model.save(GuardsDoc(templates={"theirs": (rule,)}))  # another window's template
    mine = GuardsDoc(templates={"mine": (rule,)})
    guards_model.save(mine)
    guards_model.delete_template(mine, "mine")
    assert "mine" not in mine.templates
    assert set(guards_model.load().templates) == {"theirs"}  # the other window's survives
    guards_model.save(mine)  # a later save from this window must not write it back
    assert set(guards_model.load().templates) == {"theirs"}


def test_guard_state_save_writes_only_the_dirty_accounts() -> None:
    db = shared_db.get()
    with db.change(shared_db.GUARD_STATE):
        db.put("guard_state", "theirs@X", {"day_equity": {"r": {"day": "d", "equity": 1.0}}})
    book = GuardBook(GuardsDoc(), db)
    book._day_equity["mine@Y"] = {"r2": {"day": "d", "equity": 2.0}}
    book._dirty.add("mine@Y")
    book._save()
    rows = db.load("guard_state")
    assert set(rows) == {"theirs@X", "mine@Y"}
    assert rows["theirs@X"] == {"day_equity": {"r": {"day": "d", "equity": 1.0}}}


def test_guard_book_adopts_an_account_from_the_store() -> None:
    db = shared_db.get()
    book = GuardBook(GuardsDoc(), db)
    with db.change(shared_db.GUARD_STATE):
        db.put("guard_state", "handed@X", {"latched": {"r": {"until": "hand", "at": 1.0}}})
    book._adopt("handed@X")
    assert "handed@X" in book._latched
    # But never over this window's own changes: a dirty key is ours.
    book._latched["handed@X"] = {}
    book._dirty.add("handed@X")
    book._adopt("handed@X")
    assert book._latched["handed@X"] == {}


# --- the lock ----------------------------------------------------------------------------------


def test_shared_lock_excludes_a_second_holder() -> None:
    with shared_data.shared_lock():
        with pytest.raises(OSError):  # TimeoutError is one; the callers' catches rely on it
            with shared_data.shared_lock(timeout_s=0.05):
                pass  # pragma: no cover -- acquiring twice is exactly what must not happen
    # Released: taking it again succeeds at once.
    with shared_data.shared_lock(timeout_s=0.05):
        pass


def test_a_wedged_database_does_not_lose_a_guard_state_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save the database refuses is logged and dropped -- but the keys stay dirty, so
    the very next save writes them. The midnight race's worst case, in miniature."""
    db = shared_db.get()
    book = GuardBook(GuardsDoc(), db)
    book._day_equity["mine@Y"] = {"r": {"day": "d", "equity": 2.0}}
    book._dirty.add("mine@Y")

    def wedged(*stores: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "change", wedged)
    book._save()  # swallowed and logged, never raised
    assert db.load("guard_state") == {}
    assert "mine@Y" in book._dirty
    monkeypatch.undo()
    book._save()  # the database is back: the dirty key lands
    assert "mine@Y" in db.load("guard_state")


def test_refresh_if_due_rereads_under_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second window finds the first's fresh token and does not spend the refresh token."""
    from mt5copier.ctrader import auth

    creds = auth.CTraderCredentials(
        client_id="id", client_secret="secret", access_token="old", refresh_token="r1"
    )
    creds.expires_at = time.time() + 60  # due: well inside REFRESH_BEFORE_S
    auth.save(creds)

    calls: list[str] = []

    def fake_token_call(params: dict[str, str]) -> dict[str, object]:
        calls.append(params["refresh_token"])
        return {"accessToken": "new", "refreshToken": "r2", "expiresIn": 30 * 24 * 3600}

    monkeypatch.setattr(auth, "_token_call", fake_token_call)
    assert auth.refresh_if_due() is True
    assert auth.refresh_if_due() is False  # fresh now: the re-read says not due
    assert calls == ["r1"]
    assert auth.load().refresh_token == "r2"
