"""Which slave position belongs to which master position, on disk.

**Ownership lives here, not in the order's magic number or its comment.** Both alternatives
were considered and rejected: brokers truncate and overwrite comments, and a single 64-bit
``magic`` cannot reliably carry both a copier identity *and* a master ticket. So ``magic``
carries only "this is ours", the comment is a best-effort hint for a human reading the
terminal, and this table is the authority. That is also what ``zele-mt5`` does, and it is the
one design decision of its that transfers wholesale.

The stakes are asymmetric in an unusual direction: losing this file loses no money directly,
but it orphans every live copy -- real positions stay open on real accounts with nothing
tracking them, and the next startup cannot tell them from a stranger's trades. Hence WAL,
a synchronous mode that survives a process crash, and writes that commit immediately rather
than at some convenient later point.

SQLite settings follow ``zele-mt5``'s ``core/database/service.py``, which got this part right:
``journal_mode=WAL`` so a reader never blocks the writer, ``synchronous=NORMAL`` (durable
across a process crash, which is the failure that actually happens, while not paying an fsync
per commit for a machine-level power loss), and a 30s ``busy_timeout`` so a momentary lock
waits instead of raising.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from .logging_setup import get_logger
from .protocol import OrderType, PositionType
from .reconcile import CopiedOrder, CopiedPosition

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS copies (
    terminal_id   TEXT    NOT NULL,
    master_ticket INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    slave_ticket  INTEGER,
    slave_symbol  TEXT    NOT NULL,
    direction     INTEGER NOT NULL,
    order_type    INTEGER,
    master_volume REAL    NOT NULL,
    slave_volume  REAL,
    entry         REAL    NOT NULL DEFAULT 0,
    synced_sl     REAL    NOT NULL DEFAULT 0,
    synced_tp     REAL    NOT NULL DEFAULT 0,
    opened_at     REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    PRIMARY KEY (terminal_id, master_ticket)
);
CREATE INDEX IF NOT EXISTS copies_by_slave_ticket
    ON copies (terminal_id, slave_ticket);
"""


class CopyKind(StrEnum):
    POSITION = "position"
    ORDER = "order"


@dataclass(frozen=True, slots=True)
class Copy:
    """One row: a thing we opened on a slave because of a thing on the master."""

    terminal_id: str
    master_ticket: int
    kind: CopyKind
    slave_symbol: str
    direction: PositionType
    master_volume: float
    """The master volume this copy was *sized against*, not the master's volume now. Partial
    closes are mirrored as a fraction of this, so recomputing it from current state would
    compound every previous partial."""

    slave_ticket: int | None = None
    order_type: OrderType | None = None
    slave_volume: float | None = None
    """What actually filled, which is not always what was requested."""
    entry: float = 0.0
    synced_sl: float = 0.0
    synced_tp: float = 0.0
    opened_at: float = 0.0
    updated_at: float = 0.0

    def as_position(self) -> CopiedPosition:
        return CopiedPosition(
            terminal_id=self.terminal_id,
            master_ticket=self.master_ticket,
            slave_symbol=self.slave_symbol,
            type=self.direction,
            master_volume=self.master_volume,
            synced_sl=self.synced_sl,
            synced_tp=self.synced_tp,
            slave_ticket=self.slave_ticket,
            volume=self.slave_volume,
        )

    def as_order(self) -> CopiedOrder:
        return CopiedOrder(
            terminal_id=self.terminal_id,
            master_ticket=self.master_ticket,
            slave_symbol=self.slave_symbol,
            order_type=self.order_type or OrderType.BUY_LIMIT,
            master_volume=self.master_volume,
            entry=self.entry,
            synced_sl=self.synced_sl,
            synced_tp=self.synced_tp,
            slave_ticket=self.slave_ticket,
            volume=self.slave_volume,
        )


class MappingStore:
    """The copier's record of what it owns.

    One instance per process. The connection is guarded by a lock and by the pid that opened
    it: a SQLite connection must never be used across a spawn, and this project spawns a
    worker per terminal, so an inherited handle is reopened rather than quietly corrupting
    the file.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._conn: sqlite3.Connection | None = None
        self._connect()

    # --- connection ------------------------------------------------------------------------

    def _connect(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        self._pid = os.getpid()

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None or os.getpid() != self._pid:
            # Inherited across a spawn, or closed. Reopening is the only safe move.
            log.debug("reopening mapping store in pid %s", os.getpid())
            self._connect()
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> MappingStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- writing ---------------------------------------------------------------------------

    def record(self, copy: Copy) -> Copy:
        """Insert or replace one copy. Commits immediately.

        Immediately, not batched: the window between an order filling and the fact being
        durable is a window in which a crash orphans a real position. It is one small write
        per trade, against a WAL database -- there is nothing to gain by deferring it.
        """
        now = time.time()
        stamped = Copy(
            **{
                **{f: getattr(copy, f) for f in copy.__slots__},
                "opened_at": copy.opened_at or now,
                "updated_at": now,
            }
        )
        with self._lock:
            self._db.execute(
                """
                INSERT OR REPLACE INTO copies (
                    terminal_id, master_ticket, kind, slave_ticket, slave_symbol, direction,
                    order_type, master_volume, slave_volume, entry, synced_sl, synced_tp,
                    opened_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    stamped.terminal_id,
                    stamped.master_ticket,
                    str(stamped.kind),
                    stamped.slave_ticket,
                    stamped.slave_symbol,
                    int(stamped.direction),
                    None if stamped.order_type is None else int(stamped.order_type),
                    stamped.master_volume,
                    stamped.slave_volume,
                    stamped.entry,
                    stamped.synced_sl,
                    stamped.synced_tp,
                    stamped.opened_at,
                    stamped.updated_at,
                ),
            )
            self._db.commit()
        return stamped

    def remember_position(
        self,
        *,
        terminal_id: str,
        master_ticket: int,
        slave_symbol: str,
        direction: PositionType,
        master_volume: float,
        slave_ticket: int | None = None,
        slave_volume: float | None = None,
        synced_sl: float = 0.0,
        synced_tp: float = 0.0,
    ) -> Copy:
        """Record a copied position. Keeps the original ``opened_at`` if there is one."""
        existing = self.get(terminal_id, master_ticket)
        return self.record(
            Copy(
                terminal_id=terminal_id,
                master_ticket=master_ticket,
                kind=CopyKind.POSITION,
                slave_symbol=slave_symbol,
                direction=direction,
                master_volume=master_volume,
                slave_ticket=slave_ticket,
                slave_volume=slave_volume,
                synced_sl=synced_sl,
                synced_tp=synced_tp,
                opened_at=existing.opened_at if existing else 0.0,
            )
        )

    def remember_order(
        self,
        *,
        terminal_id: str,
        master_ticket: int,
        slave_symbol: str,
        direction: PositionType,
        order_type: OrderType,
        master_volume: float,
        entry: float = 0.0,
        slave_ticket: int | None = None,
        slave_volume: float | None = None,
        synced_sl: float = 0.0,
        synced_tp: float = 0.0,
    ) -> Copy:
        existing = self.get(terminal_id, master_ticket)
        return self.record(
            Copy(
                terminal_id=terminal_id,
                master_ticket=master_ticket,
                kind=CopyKind.ORDER,
                slave_symbol=slave_symbol,
                direction=direction,
                order_type=order_type,
                master_volume=master_volume,
                entry=entry,
                slave_ticket=slave_ticket,
                slave_volume=slave_volume,
                synced_sl=synced_sl,
                synced_tp=synced_tp,
                opened_at=existing.opened_at if existing else 0.0,
            )
        )

    def update(self, terminal_id: str, master_ticket: int, **changes: object) -> Copy | None:
        """Read-modify-write one copy. Returns the new row, or None if it was not there.

        Read-modify-write rather than a partial UPDATE so the row that lands is always a
        complete, validated :class:`Copy` -- there is no path by which half a row gets written.
        """
        current = self.get(terminal_id, master_ticket)
        if current is None:
            return None
        return self.record(replace(current, **changes))  # type: ignore[arg-type]

    def forget(self, terminal_id: str, master_ticket: int) -> None:
        """Drop one copy -- it closed, or was cancelled."""
        with self._lock:
            self._db.execute(
                "DELETE FROM copies WHERE terminal_id = ? AND master_ticket = ?",
                (terminal_id, master_ticket),
            )
            self._db.commit()

    def forget_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM copies WHERE terminal_id = ?", (terminal_id,))
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM copies")
            self._db.commit()

    # --- reading ---------------------------------------------------------------------------

    def get(self, terminal_id: str, master_ticket: int) -> Copy | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM copies WHERE terminal_id = ? AND master_ticket = ?",
                (terminal_id, master_ticket),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def by_slave_ticket(self, terminal_id: str, slave_ticket: int) -> Copy | None:
        """The reverse lookup startup reconciliation needs: this position exists on the
        slave -- is it one of ours?"""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM copies WHERE terminal_id = ? AND slave_ticket = ?",
                (terminal_id, slave_ticket),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def for_terminal(self, terminal_id: str, kind: CopyKind | None = None) -> list[Copy]:
        sql = "SELECT * FROM copies WHERE terminal_id = ?"
        args: list[object] = [terminal_id]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(str(kind))
        sql += " ORDER BY master_ticket"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [_from_row(r) for r in rows]

    def all(self) -> list[Copy]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM copies ORDER BY terminal_id, master_ticket"
            ).fetchall()
        return [_from_row(r) for r in rows]

    # --- views the diff engine wants ---------------------------------------------------------

    def positions(self, terminal_id: str | None = None) -> list[CopiedPosition]:
        rows = self.all() if terminal_id is None else self.for_terminal(terminal_id)
        return [c.as_position() for c in rows if c.kind is CopyKind.POSITION]

    def orders(self, terminal_id: str | None = None) -> list[CopiedOrder]:
        rows = self.all() if terminal_id is None else self.for_terminal(terminal_id)
        return [c.as_order() for c in rows if c.kind is CopyKind.ORDER]

    def get_position(self, terminal_id: str, master_ticket: int) -> CopiedPosition | None:
        row = self.get(terminal_id, master_ticket)
        return row.as_position() if row is not None and row.kind is CopyKind.POSITION else None

    def terminals(self) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT terminal_id FROM copies").fetchall()
        return {r["terminal_id"] for r in rows}


@dataclass(frozen=True, slots=True)
class Orphan:
    """A position on a slave that carries our magic but is not in the table.

    Surfaced, never adopted and never closed automatically. Adopting it means managing a
    position whose sizing basis is unknown; closing it means acting on something that might
    not be ours at all. Both are the user's call, and they need to be told it exists.
    """

    terminal_id: str
    slave_ticket: int
    symbol: str
    volume: float


def find_orphans(
    store: MappingStore,
    terminal_id: str,
    slave_positions: Iterable[tuple[int, str, float, int]],
    copier_magic: int,
) -> list[Orphan]:
    """Positions on this slave that we appear to have opened but do not have recorded.

    ``slave_positions`` is ``(ticket, symbol, volume, magic)`` for each open position on the
    slave. Only those carrying our magic are candidates: anything else is the user's own
    trading and none of our business.
    """
    out: list[Orphan] = []
    for ticket, symbol, volume, magic in slave_positions:
        if magic != copier_magic:
            continue
        if store.by_slave_ticket(terminal_id, ticket) is not None:
            continue
        out.append(Orphan(terminal_id, ticket, symbol, volume))
    return out


def find_missing(
    store: MappingStore,
    terminal_id: str,
    slave_position_tickets: Iterable[int],
) -> list[Copy]:
    """Copies we have recorded whose slave position is gone.

    Closed by hand, stopped out, or hit its take-profit while we were not running. The record
    is stale and must not be used to derive a close -- there is nothing left to close.
    """
    live = set(slave_position_tickets)
    return [
        c
        for c in store.for_terminal(terminal_id, CopyKind.POSITION)
        if c.slave_ticket is not None and c.slave_ticket not in live
    ]


def _from_row(row: sqlite3.Row) -> Copy:
    return Copy(
        terminal_id=row["terminal_id"],
        master_ticket=int(row["master_ticket"]),
        kind=CopyKind(row["kind"]),
        slave_symbol=row["slave_symbol"],
        direction=PositionType(int(row["direction"])),
        master_volume=float(row["master_volume"]),
        slave_ticket=None if row["slave_ticket"] is None else int(row["slave_ticket"]),
        order_type=None if row["order_type"] is None else OrderType(int(row["order_type"])),
        slave_volume=None if row["slave_volume"] is None else float(row["slave_volume"]),
        entry=float(row["entry"]),
        synced_sl=float(row["synced_sl"]),
        synced_tp=float(row["synced_tp"]),
        opened_at=float(row["opened_at"]),
        updated_at=float(row["updated_at"]),
    )
