"""``shared.db``: the one database every profile reads and writes together.

The four mutable shared stores -- account names, guard rules and templates, the guards'
recorded state, and the prop account containers -- used to be four JSON files under
``shared\\``, each read-modify-written under one folder-wide mutex. They are now tables in
one SQLite file. The database's own locking replaces the mutex (:meth:`SharedDb.change`
takes the writer's seat with ``BEGIN IMMEDIATE``, so a read inside it sees the latest
committed state and nothing writes between that read and the commit), and a change that
touches two stores -- a guard rule and its state, an account row and a template -- lands in
one transaction instead of two file replaces a crash could split.

**The shape is a document store, on purpose.** Every table is ``(key, value)`` with the
value as JSON, exactly what the file held per key; the modules that own each store keep
their parsing and their models, and read rows instead of a file. A store that one day needs
querying can be promoted to real columns in a migration of its own, one table at a time,
without touching the others.

**Change detection** replaces the files' mtime polling: the ``meta`` table carries a stamp
per store, bumped inside every :meth:`~SharedDb.change`, and a poll is one indexed SELECT.
Stamps are nanosecond wall-clock at the first write and strictly increasing after it, so a
stamp cached across a database swapped under the process (the tests do this) can never
collide with the new file's.

The JSON files stay on disk, untouched: :func:`get` imports them into a fresh database
once (a ``meta`` marker guards the import, inside the same transaction as the rows), and
they remain the rollback story, as each profile's own files did for the shared folder.
What stays JSON stays for a reason: ``news.json`` is rewritten whole from one fetch,
``machine.json`` is last-write-wins by design, ``catalog.json`` and ``ctrader.json`` keep
:func:`mt5copier.shared_data.shared_lock` for their whole-file read-modify-writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

from .json_store import read_json
from .logging_setup import get_logger
from .paths import (
    account_names_path,
    guard_state_path,
    guards_path,
    prop_accounts_path,
    shared_db_path,
)
from .sqlite_store import SqliteStore

log = get_logger(__name__)

NAMES = "names"
GUARDS = "guards"
"""One stamp for the rules and the templates together: every save writes both."""
GUARD_STATE = "guard_state"
PROPS = "props"
POLICIES = "policies"
"""Each account's remembered trader-policy settings (:data:`mt5copier.config.POLICY_FIELDS`),
keyed by account. Machine-wide on purpose: a profile owns terminals, never an account's
tuning, so a new profile sees every known account already tuned. Born in the database --
there was never a JSON file to import."""
OWNERS = "owners"
"""Which profile owns each account -- see :mod:`mt5copier.ownership`. Persistent data, not
a runtime lock: ownership survives the owning window closing, and every other profile sees
the account read-only until the owner lets go. Born in the database too."""

_IMPORTED = "json_imported"
"""The ``meta`` store name whose presence says the one-time JSON import has run."""

_TABLES = frozenset(
    {
        "account_names",
        "account_policies",
        "guard_accounts",
        "guard_templates",
        "guard_state",
        "prop_containers",
        "account_owners",
    }
)


def _table(name: str) -> str:
    """The table name, vouched for -- every query interpolates through here, so a table
    name can never be anything but one of the literals above."""
    if name not in _TABLES:
        raise ValueError(f"no shared table called {name!r}")
    return name


class SharedDb(SqliteStore):
    """The shared stores as one SQLite file. See the module docstring for why and shape.

    Reading (:meth:`load`, :meth:`fetch`, :meth:`stamp`) needs no ceremony. Writing goes
    through :meth:`change`: ``put`` and ``drop`` refuse to run outside one, so a slipped
    call cannot leave an implicit transaction holding the write lock open forever.
    """

    SCHEMA: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS meta(store TEXT PRIMARY KEY, stamp INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS account_names(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_policies(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS guard_accounts(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS guard_templates(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS guard_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS prop_containers(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_owners(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """

    @property
    def path(self) -> str:
        return self._path

    def stamp(self, store: str) -> int:
        """When this store last changed, as a strictly increasing integer; 0 when it has
        never been written. One SELECT, cheap enough for every poll tick."""
        with self._lock:
            row = self._db.execute("SELECT stamp FROM meta WHERE store=?", (store,)).fetchone()
            return int(row["stamp"]) if row else 0

    def load(self, table: str) -> dict[str, Any]:
        """Every row of one table, values parsed from JSON. A row that cannot be parsed is
        skipped and said in the log, never fatal -- the forgiving reading every shared
        store has always had."""
        t = _table(table)
        with self._lock:
            rows = self._db.execute(f"SELECT key, value FROM {t}").fetchall()  # noqa: S608
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except ValueError:
                log.warning("%s row %r in shared.db is unreadable; skipped", t, row["key"])
        return out

    def fetch(self, table: str, key: str) -> Any | None:
        """One row's parsed value, or None when the key is absent or unreadable."""
        t = _table(table)
        with self._lock:
            row = self._db.execute(
                f"SELECT value FROM {t} WHERE key=?",  # noqa: S608
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            log.warning("%s row %r in shared.db is unreadable; treated as absent", t, key)
            return None

    @contextmanager
    def change(self, *stores: str) -> Iterator[None]:
        """One read-modify-write against the shared stores, atomic across every table it
        touches. ``BEGIN IMMEDIATE`` takes the database's write lock before the caller
        reads, so what it reads is what it replaces -- the role the shared-folder mutex
        used to play, now held by SQLite with its 30s busy wait. On the way out the named
        stores' stamps are bumped and everything commits together; any exception rolls the
        whole change back."""
        with self._lock:
            conn = self._db
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                now = time.time_ns()
                for store in stores:
                    conn.execute(
                        "INSERT INTO meta(store, stamp) VALUES(?, ?) "
                        "ON CONFLICT(store) DO UPDATE SET stamp = MAX(stamp + 1, excluded.stamp)",
                        (store, now),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def put(self, table: str, key: str, value: Any) -> None:
        """Set one row, inside a :meth:`change` only."""
        t = _table(table)
        with self._lock:
            conn = self._db
            if not conn.in_transaction:
                raise RuntimeError("shared.db write outside change()")
            conn.execute(
                f"INSERT OR REPLACE INTO {t}(key, value) VALUES(?, ?)",  # noqa: S608
                (key, json.dumps(value)),
            )

    def drop(self, table: str, key: str) -> None:
        """Delete one row, inside a :meth:`change` only."""
        t = _table(table)
        with self._lock:
            conn = self._db
            if not conn.in_transaction:
                raise RuntimeError("shared.db write outside change()")
            conn.execute(f"DELETE FROM {t} WHERE key=?", (key,))  # noqa: S608


# --- the process-wide handle ------------------------------------------------------------------

_get_lock = threading.Lock()
_db: SharedDb | None = None


def get() -> SharedDb:
    """The process's handle on the shared database, importing the JSON stores on the way
    in the first time this database file is seen. Re-resolved against the current data dir
    on every call, so the tests' isolated ``%APPDATA%`` gets a fresh database per test and
    the real app pays one string compare."""
    global _db
    path = str(shared_db_path())
    with _get_lock:
        if _db is None or _db.path != path:
            if _db is not None:
                _db.close()
            db = SharedDb(path)
            try:
                _import_json(db)
            except sqlite3.Error:
                log.exception("could not import the shared JSON stores into shared.db")
            _db = db
        return _db


def _import_json(db: SharedDb) -> None:
    """Carry the four JSON stores into a database that has never held them, once.

    Raw dicts in, rows out -- no models: another version of the app may have written the
    files, and the stores' own readers already forgive what they cannot parse. The files
    are left exactly where they were, as the rollback story. Two windows first-launching
    together both reach here; ``change`` serialises them and the import is idempotent, so
    the worst case is the same rows written twice.
    """
    if db.stamp(_IMPORTED):
        return

    def entries(raw: object, field: str | None = None) -> dict[str, Any]:
        if field is not None:
            raw = raw.get(field) if isinstance(raw, dict) else None
        return raw if isinstance(raw, dict) else {}

    names = {
        k: v for k, v in entries(read_json(account_names_path())).items() if isinstance(v, str)
    }
    guards_raw = read_json(guards_path())
    accounts = entries(guards_raw, "accounts")
    templates = entries(guards_raw, "templates")
    state_raw = read_json(guard_state_path())
    state: dict[str, dict[str, Any]] = {}
    for section in ("day_equity", "latched", "opens"):
        for key, held in entries(state_raw, section).items():
            state.setdefault(key, {})[section] = held
    containers = entries(read_json(prop_accounts_path()), "containers")

    found = (
        (NAMES, names),
        (GUARDS, accounts or templates),
        (GUARD_STATE, state),
        (PROPS, containers),
    )
    stores = [_IMPORTED] + [store for store, got in found if got]
    with db.change(*stores):
        for key, name in names.items():
            db.put("account_names", key, name)
        for key, value in accounts.items():
            db.put("guard_accounts", key, value)
        for key, value in templates.items():
            db.put("guard_templates", key, value)
        for key, value in state.items():
            db.put("guard_state", key, value)
        for key, value in containers.items():
            db.put("prop_containers", key, value)
    if len(stores) > 1:
        log.info("shared stores imported into %s from their JSON files", db.path)
