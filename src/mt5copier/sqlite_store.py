"""The SQLite plumbing the on-disk stores share.

:class:`~mt5copier.store.MappingStore` and :class:`~mt5copier.history.HistoryStore` each own
one file, and what they keep in it could not be more different -- one is the authority on
what the copier owns, the other a display-only diary. But how they hold the file is the same,
and lives here: a subclass supplies its schema and the columns added since it first shipped,
and inherits everything about opening, reopening, and closing the connection.

The settings are the careful standard ones:
``journal_mode=WAL`` so a reader never blocks the writer, ``synchronous=NORMAL`` (durable
across a process crash, which is the failure that actually happens, while not paying an fsync
per commit for a machine-level power loss), and a 30s ``busy_timeout`` so a momentary lock
waits instead of raising.

Opening is idempotent and converges any older file: ``CREATE TABLE IF NOT EXISTS`` plus an
``ALTER TABLE ADD COLUMN`` for each :attr:`~SqliteStore.ADDED_COLUMNS` entry the file lacks.
A user can skip versions in either direction and the schema is right after the first open.

A connection must never be used across a spawn, and this project spawns a worker per
terminal, so the handle remembers the pid that opened it and an inherited one is reopened
rather than quietly corrupting the file. What each subclass does when a statement fails
stays its own: the mapping store lets the error out, the history logs and drops it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import ClassVar, Self

from .logging_setup import get_logger

log = get_logger(__name__)


class SqliteStore:
    """One SQLite file, held safely across threads and spawns. Subclasses set
    :attr:`SCHEMA` and, as the schema grows, :attr:`ADDED_COLUMNS`."""

    SCHEMA: ClassVar[str] = ""
    """Idempotent DDL (``CREATE ... IF NOT EXISTS``) run on every open."""

    ADDED_COLUMNS: ClassVar[tuple[tuple[str, str, str], ...]] = ()
    """``(table, column, declaration)`` for each column added after its table first shipped.
    ``CREATE TABLE IF NOT EXISTS`` leaves an older file's table as it was, so each is added
    on open when it is missing. Declarations need a default that reads as "not known"."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(self.SCHEMA)
        have: dict[str, set[str]] = {}
        for table, name, decl in self.ADDED_COLUMNS:
            if table not in have:
                have[table] = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if name not in have[table]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.commit()
        self._conn = conn
        self._pid = os.getpid()
        return conn

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None or os.getpid() != self._pid:
            if self._conn is not None:
                log.debug("reopening %s in pid %s", type(self).__name__, os.getpid())
            return self._connect()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
