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

How the file is held -- WAL, ``synchronous=NORMAL``, the 30s busy timeout, the reopen after
a spawn -- is :mod:`mt5copier.sqlite_store`'s, shared with the trade history.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from .protocol import OrderType, PositionType
from .reconcile import CopiedOrder, CopiedPosition
from .sqlite_store import SqliteStore

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
    synced_expiration INTEGER NOT NULL DEFAULT 0,
    point         REAL    NOT NULL DEFAULT 0,
    digits        INTEGER NOT NULL DEFAULT 0,
    risk_distance REAL    NOT NULL DEFAULT 0,
    price_value   REAL    NOT NULL DEFAULT 0,
    commission_per_lot REAL NOT NULL DEFAULT 0,
    master_managed INTEGER NOT NULL DEFAULT -1,
    breakeven_sl  REAL    NOT NULL DEFAULT 0,
    breakeven_exempt INTEGER NOT NULL DEFAULT 0,
    breakeven_at  REAL    NOT NULL DEFAULT 0,
    book_fraction REAL    NOT NULL DEFAULT 0,
    book_tries    INTEGER NOT NULL DEFAULT 0,
    booked        INTEGER NOT NULL DEFAULT 0,
    guard_close   INTEGER NOT NULL DEFAULT 0,
    opened_at     REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    PRIMARY KEY (terminal_id, master_ticket)
);
CREATE INDEX IF NOT EXISTS copies_by_slave_ticket
    ON copies (terminal_id, slave_ticket);
CREATE TABLE IF NOT EXISTS master_trades (
    terminal_id    TEXT    NOT NULL,
    ticket         INTEGER NOT NULL,
    risk_distance  REAL    NOT NULL DEFAULT 0,
    breakeven_done INTEGER NOT NULL DEFAULT 0,
    booked         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (terminal_id, ticket)
);
"""

_ADDED_COLUMNS = (
    ("copies", "point", "REAL NOT NULL DEFAULT 0"),
    ("copies", "digits", "INTEGER NOT NULL DEFAULT 0"),
    ("copies", "risk_distance", "REAL NOT NULL DEFAULT 0"),
    ("copies", "price_value", "REAL NOT NULL DEFAULT 0"),
    ("copies", "breakeven_sl", "REAL NOT NULL DEFAULT 0"),
    ("copies", "breakeven_exempt", "INTEGER NOT NULL DEFAULT 0"),
    ("copies", "breakeven_at", "REAL NOT NULL DEFAULT 0"),
    ("copies", "book_fraction", "REAL NOT NULL DEFAULT 0"),
    ("copies", "book_tries", "INTEGER NOT NULL DEFAULT 0"),
    ("copies", "booked", "INTEGER NOT NULL DEFAULT 0"),
    ("copies", "commission_per_lot", "REAL NOT NULL DEFAULT 0"),
    ("copies", "master_managed", "INTEGER NOT NULL DEFAULT -1"),
    ("copies", "guard_close", "INTEGER NOT NULL DEFAULT 0"),
    ("copies", "synced_expiration", "INTEGER NOT NULL DEFAULT 0"),
)
"""Columns added after the table first shipped, for breakeven and booking at it. The defaults
read as "not known" and "not triggered", which is what an older copy is."""


@dataclass(frozen=True, slots=True)
class MasterTrade:
    """What the master's own breakeven remembers about one of its trades, so that it fires
    once, survives a restart, and measures R from the stop the trade first had."""

    terminal_id: str
    ticket: int
    risk_distance: float = 0.0
    """The distance from its entry to the first stop it was seen with: one R. 0 while it has
    had none."""
    breakeven_done: bool = False
    booked: bool = False
    """The booking at breakeven has gone out, answered or not: it is never sent twice."""


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
    synced_expiration: int = 0
    """An order row only: the broker-side expiry the copy carries, in real UTC seconds."""
    point: float = 0.0
    digits: int = 0
    risk_distance: float = 0.0
    price_value: float = 0.0
    commission_per_lot: float = 0.0
    master_managed: bool | None = None
    """Whether the master manages the copy. None for a row written before that was kept,
    until :meth:`MappingStore.settle_management` decides it once."""
    breakeven_sl: float = 0.0
    breakeven_exempt: bool = False
    breakeven_at: float = 0.0
    book_fraction: float = 0.0
    book_tries: int = 0
    booked: bool = False
    guard_close: bool = False
    """A guard closes (or cancels) this copy, whatever the master does: see
    :attr:`mt5copier.reconcile.CopiedPosition.guard_close`."""
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
            point=self.point,
            digits=self.digits,
            risk_distance=self.risk_distance,
            price_value=self.price_value,
            commission_per_lot=self.commission_per_lot,
            master_managed=self.master_managed is not False,
            breakeven_sl=self.breakeven_sl,
            breakeven_exempt=self.breakeven_exempt,
            breakeven_at=self.breakeven_at,
            book_fraction=self.book_fraction,
            book_tries=self.book_tries,
            booked=self.booked,
            guard_close=self.guard_close,
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
            synced_expiration=self.synced_expiration,
            slave_ticket=self.slave_ticket,
            volume=self.slave_volume,
            point=self.point,
            digits=self.digits,
            risk_distance=self.risk_distance,
            price_value=self.price_value,
            master_managed=self.master_managed is not False,
            guard_close=self.guard_close,
        )


class MappingStore(SqliteStore):
    """The copier's record of what it owns.

    One instance per process; how the file is held is :class:`SqliteStore`'s. It opens
    eagerly: a mapping store that cannot open its file must fail at startup, not on the
    first trade.
    """

    SCHEMA = _SCHEMA
    ADDED_COLUMNS = _ADDED_COLUMNS

    def __init__(self, path: Path | str = ":memory:") -> None:
        super().__init__(path)
        self._connect()

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
                    synced_expiration,
                    point, digits, risk_distance, price_value, commission_per_lot,
                    master_managed, breakeven_sl, breakeven_exempt, breakeven_at,
                    book_fraction, book_tries, booked, guard_close, opened_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    stamped.synced_expiration,
                    stamped.point,
                    stamped.digits,
                    stamped.risk_distance,
                    stamped.price_value,
                    stamped.commission_per_lot,
                    -1 if stamped.master_managed is None else int(stamped.master_managed),
                    stamped.breakeven_sl,
                    int(stamped.breakeven_exempt),
                    stamped.breakeven_at,
                    stamped.book_fraction,
                    stamped.book_tries,
                    int(stamped.booked),
                    int(stamped.guard_close),
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
        point: float = 0.0,
        digits: int = 0,
        risk_distance: float = 0.0,
        price_value: float = 0.0,
        commission_per_lot: float = 0.0,
        master_managed: bool = True,
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
                point=point,
                digits=digits,
                risk_distance=risk_distance,
                price_value=price_value,
                commission_per_lot=commission_per_lot,
                master_managed=master_managed,
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
        synced_expiration: int = 0,
        point: float = 0.0,
        digits: int = 0,
        risk_distance: float = 0.0,
        price_value: float = 0.0,
        master_managed: bool = True,
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
                synced_expiration=synced_expiration,
                point=point,
                digits=digits,
                risk_distance=risk_distance,
                price_value=price_value,
                master_managed=master_managed,
                opened_at=existing.opened_at if existing else 0.0,
            )
        )

    def settle_management(self, decide: Callable[[str], bool]) -> int:
        """Decide, once, who manages each copy recorded before that was kept: ``decide`` is
        given the slave's terminal id. How many rows were settled."""
        with self._lock:
            tids = [
                str(r[0])
                for r in self._db.execute(
                    "SELECT DISTINCT terminal_id FROM copies WHERE master_managed = -1"
                )
            ]
            settled = 0
            for tid in tids:
                cursor = self._db.execute(
                    "UPDATE copies SET master_managed = ? "
                    "WHERE terminal_id = ? AND master_managed = -1",
                    (int(decide(tid)), tid),
                )
                settled += cursor.rowcount
            self._db.commit()
        return settled

    def master_trades(self, terminal_id: str) -> dict[int, MasterTrade]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM master_trades WHERE terminal_id = ?", (terminal_id,)
            ).fetchall()
        return {
            int(r["ticket"]): MasterTrade(
                terminal_id=r["terminal_id"],
                ticket=int(r["ticket"]),
                risk_distance=float(r["risk_distance"]),
                breakeven_done=bool(r["breakeven_done"]),
                booked=bool(r["booked"]),
            )
            for r in rows
        }

    def remember_master_trade(self, trade: MasterTrade) -> MasterTrade:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO master_trades "
                "(terminal_id, ticket, risk_distance, breakeven_done, booked) VALUES (?,?,?,?,?)",
                (
                    trade.terminal_id,
                    trade.ticket,
                    trade.risk_distance,
                    int(trade.breakeven_done),
                    int(trade.booked),
                ),
            )
            self._db.commit()
        return trade

    def forget_master_trades(self, terminal_id: str, keep: Iterable[int]) -> None:
        """Drop the master's trades that are no longer open: every one not in ``keep``."""
        kept = sorted(set(keep))
        marks = ",".join("?" * len(kept))
        where = f" AND ticket NOT IN ({marks})" if kept else ""
        with self._lock:
            self._db.execute(
                f"DELETE FROM master_trades WHERE terminal_id = ?{where}", (terminal_id, *kept)
            )
            self._db.commit()

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


def find_missing_orders(
    store: MappingStore,
    terminal_id: str,
    slave_order_tickets: Iterable[int],
    slave_position_tickets: Iterable[int],
) -> list[Copy]:
    """Order copies whose slave order is neither still resting nor filled into a position.

    **A filled order is not missing, and this is the whole reason the function takes two
    ticket lists.** In MT5 a pending order that fills becomes a position carrying the *same*
    ticket, so an order row whose ticket now names a live position has not gone anywhere -- it
    has been filled, and ``reconcile_slave_orders`` is waiting to report ``ORDER_FILLED`` and
    convert the row in place. Forgetting it here would throw away the handover and copy the
    trade a second time as a fresh open.

    What is left after both checks is genuinely gone: cancelled by hand, expired, or rejected
    while we were not running.
    """
    resting = set(slave_order_tickets)
    filled = set(slave_position_tickets)
    return [
        c
        for c in store.for_terminal(terminal_id, CopyKind.ORDER)
        if c.slave_ticket is not None
        and c.slave_ticket not in resting
        and c.slave_ticket not in filled
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
        synced_expiration=int(row["synced_expiration"]),
        point=float(row["point"]),
        digits=int(row["digits"]),
        risk_distance=float(row["risk_distance"]),
        price_value=float(row["price_value"]),
        commission_per_lot=float(row["commission_per_lot"]),
        master_managed=None if int(row["master_managed"]) < 0 else bool(row["master_managed"]),
        breakeven_sl=float(row["breakeven_sl"]),
        breakeven_exempt=bool(row["breakeven_exempt"]),
        breakeven_at=float(row["breakeven_at"]),
        book_fraction=float(row["book_fraction"]),
        book_tries=int(row["book_tries"]),
        booked=bool(row["booked"]),
        guard_close=bool(row["guard_close"]),
        opened_at=float(row["opened_at"]),
        updated_at=float(row["updated_at"]),
    )
