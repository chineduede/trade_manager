"""Each trade's history, per slave, on disk: every step the copier took with it.

The day book answers "what did every slave make of what the master just did", for this session.
This answers the question the other way round and after the fact: "what became of master trade
#1984999641 on BlackBull, from start to finish?" -- copied with how many lots, a partial close,
a stop moved, a breakeven, a change declined and why, closed. One row per step, written as
each step settles, grouped by slave and master ticket when read. Only trades actually copied to
the slave are read back: one the slave never took is a line in the log, not a trade of its.

Display only: nothing that decides what to send may read it. Steps are kept for
``AppConfig.history_days`` and pruned on opening and about hourly after.

Settings follow :mod:`mt5copier.store`'s (WAL, ``synchronous=NORMAL``, a 30s busy timeout), but
losing this file loses nothing the copier needs, so a write that fails is logged and dropped.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from .day_book import MasterEvent, Outcome, SlaveOutcome
from .logging_setup import get_logger
from .protocol import ActionKind
from .reconcile import Action

log = get_logger(__name__)

NOTE = "note"
"""The ``kind`` of a step the copier took by itself rather than for a master event, such as a
copy reaching breakeven."""

_PRUNE_EVERY = 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    terminal_id   TEXT    NOT NULL,
    master_ticket INTEGER NOT NULL,
    symbol        TEXT    NOT NULL,
    label         TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    problem       INTEGER NOT NULL,
    master        TEXT    NOT NULL,
    slave         TEXT    NOT NULL,
    lots          REAL,
    action        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS steps_by_trade ON steps (terminal_id, master_ticket, ts);
CREATE INDEX IF NOT EXISTS steps_by_time ON steps (ts);
"""

_ADDED_COLUMNS = (("lots", "REAL"), ("action", "TEXT NOT NULL DEFAULT ''"))
"""Columns added after the table first shipped, added on open to a file that lacks them."""

_ENTRIES = (ActionKind.OPEN.value, ActionKind.PLACE.value)

ORPHANED = "orphaned"
"""The action of the note written when a settings change gives a copy up. It ends the trade's
history on that slave: nothing is recorded for it afterwards, because nothing manages it."""
_TAKEN = (Outcome.DONE.value, Outcome.DRY_RUN.value)


@dataclass(frozen=True, slots=True)
class Step:
    ts: float
    terminal_id: str
    master_ticket: int
    symbol: str
    """The master's symbol, which names the trade on every slave."""
    label: str
    """``buy``/``sell``, or the resting order's kind."""
    kind: str
    """An :class:`ActionKind` value, or :data:`NOTE`."""
    outcome: str
    """An :class:`Outcome` value; empty for a note."""
    problem: bool
    """The master did something this slave did not: refused, failed, never answered."""
    master: str
    """The master's side where it adds to the slave's: its lots and price on an entry, what
    it closed down from on a partial close. Empty otherwise."""
    slave: str
    """The details of what the copier did on this slave, and how that went."""
    lots: float | None = None
    """What the broker filled, when it says."""
    action: str = ""
    """What was done, in a word or few: ``opened``, ``stop/target change skipped``,
    ``breakeven``. Empty on a step written before it was kept apart, whose details say it."""


@dataclass(frozen=True, slots=True)
class Trade:
    """One master trade as seen on one slave."""

    terminal_id: str
    master_ticket: int
    symbol: str
    label: str
    steps: tuple[Step, ...]
    """Oldest first."""

    @property
    def first(self) -> float:
        return self.steps[0].ts

    @property
    def last(self) -> float:
        return self.steps[-1].ts

    @property
    def problems(self) -> int:
        return sum(1 for s in self.steps if s.problem)

    @property
    def entry(self) -> Step | None:
        """The step that copied it to the slave."""
        return next((s for s in self.steps if s.kind in _ENTRIES and s.outcome in _TAKEN), None)

    @property
    def status(self) -> str:
        """Where the trade stands on the slave, in a word or two, read from its steps."""
        done = {s.kind for s in self.steps if s.outcome in (Outcome.DONE, Outcome.DRY_RUN)}
        if ActionKind.CLOSE in done or ActionKind.CANCEL in done:
            return "closed" if ActionKind.CLOSE in done else "cancelled"
        if any(s.kind == NOTE and s.action == ORPHANED for s in self.steps):
            return ORPHANED
        if ActionKind.OPEN in done or ActionKind.ORDER_FILLED in done:
            return "open"
        if ActionKind.PLACE in done:
            return "resting"
        entry = next((s for s in self.steps if s.kind in (ActionKind.OPEN, ActionKind.PLACE)), None)
        if entry is not None and entry.outcome != Outcome.SENDING:
            return "not copied"
        return "seen"


# --- wording ---------------------------------------------------------------------------------


def _price(value: float | None) -> str:
    if not value:
        return "none"
    return f"{value:,.5f}".rstrip("0").rstrip(".")


def _lots(value: float | None) -> str:
    return f"{value:g} lots" if value is not None else "? lots"


def _timing(outcome: SlaveOutcome) -> str:
    text = ""
    if outcome.latency_ms is not None:
        text += f" in {outcome.latency_ms:,.0f} ms"
    if outcome.attempts > 1:
        text += f" ({outcome.attempts} tries)"
    return text


_NOUN = {
    ActionKind.OPEN: "copy",
    ActionKind.CLOSE: "close",
    ActionKind.REDUCE: "partial close",
    ActionKind.BOOK: "profit booking",
    ActionKind.MODIFY_SLTP: "stop/target change",
    ActionKind.PLACE: "order",
    ActionKind.MODIFY_ORDER: "order change",
    ActionKind.CANCEL: "cancel",
}
"""What a step that did not go through was, so ``skipped: its slave manages this copy`` says what it was
that was skipped."""


def master_side(head: MasterEvent) -> str:
    """The master's lots and price on an entry, ``0.78 -> 0.5 lots`` on a partial close."""
    if head.kind in (ActionKind.OPEN, ActionKind.PLACE):
        return f"master {head.volume:g} lots at {_price(head.entry)}"
    if head.kind is ActionKind.REDUCE:
        if head.volume_before is not None:
            return f"master {head.volume_before:g} -> {head.volume:g} lots"
        return f"master left with {head.volume:g} lots"
    return ""


_DONE = {
    ActionKind.OPEN: "opened",
    ActionKind.PLACE: "placed",
    ActionKind.CLOSE: "closed",
    ActionKind.REDUCE: "partly closed",
    ActionKind.BOOK: "profit booked",
    ActionKind.MODIFY_SLTP: "stop/target moved",
    ActionKind.MODIFY_ORDER: "order changed",
    ActionKind.CANCEL: "cancelled",
    ActionKind.ORDER_FILLED: "order filled",
}


def describe_step(
    kind: ActionKind, outcome: SlaveOutcome, action: Action | None
) -> tuple[str, str]:
    """What the copier did on the slave, as the action and its details: ``("opened", "1.37 lots
    at 86,019, stop 85,158.9, target 86,845.2 in 120 ms, 2 points worse than the master")``.
    The levels are the slave's own, so a reversed copy or a breakeven stop reads as it is on
    the slave, not as the master has it."""
    noun = _NOUN.get(kind, kind.value.replace("_", " "))
    o = outcome.outcome
    if o is Outcome.DONE and outcome.target_gone:
        return noun, "not needed: the copy was already gone from the slave"
    if o is Outcome.FOLLOWS:
        return "order filled", "the slave's own copy of the order filled with it"
    if o is Outcome.DRY_RUN:
        where = f" to {action.slave_symbol}" if action is not None else ""
        return f"{noun} (dry run)", f"nothing sent{where}"
    if o is not Outcome.DONE:
        detail = outcome.reason or ("waiting for the slave" if o is Outcome.SENDING else "")
        if kind is ActionKind.MODIFY_SLTP and action is not None:
            asked = f"asked for stop {_price(action.sl)}, target {_price(action.tp)}"
            detail = f"{detail} ({asked})" if detail else asked
        return f"{noun} {o.value}", detail or "no reason given"

    verb = _DONE.get(kind, "done")
    at = f" at {_price(outcome.price)}" if outcome.price else ""
    if kind is ActionKind.OPEN:
        text = f"{_lots(outcome.lots)}{at}"
        if action is not None:
            text += f", stop {_price(action.sl)}, target {_price(action.tp)}"
        text += _timing(outcome)
        slip = outcome.slippage_points
        if slip is not None and round(slip) != 0:
            text += f", {abs(slip):,.0f} points {'worse' if slip > 0 else 'better'} than the master"
        return verb, text
    if kind is ActionKind.PLACE:
        text = _lots(outcome.lots)
        if action is not None:
            text += (
                f" at {_price(action.entry)}, stop {_price(action.sl)}, target {_price(action.tp)}"
            )
        return verb, text + _timing(outcome)
    if kind in (ActionKind.CLOSE, ActionKind.REDUCE, ActionKind.BOOK):
        return verb, f"{_lots(outcome.lots)}{at}{_timing(outcome)}"
    if kind is ActionKind.MODIFY_SLTP and action is not None:
        return verb, f"stop {_price(action.sl)}, target {_price(action.tp)}{_timing(outcome)}"
    if kind is ActionKind.MODIFY_ORDER and action is not None:
        return verb, (
            f"to {_price(action.entry)}, stop {_price(action.sl)}, "
            f"target {_price(action.tp)}{_timing(outcome)}"
        )
    return verb, _timing(outcome).strip()


def step_for(head: MasterEvent, outcome: SlaveOutcome, action: Action | None) -> Step:
    did, detail = describe_step(head.kind, outcome, action)
    return Step(
        ts=outcome.ts if outcome.ts is not None else head.ts,
        terminal_id=outcome.terminal_id,
        master_ticket=head.master_ticket,
        symbol=head.symbol,
        label=head.label,
        kind=head.kind.value,
        outcome=outcome.outcome.value,
        problem=outcome.outcome.problem,
        master=master_side(head),
        slave=detail,
        lots=outcome.lots,
        action=did,
    )


# --- the store -------------------------------------------------------------------------------


class HistoryStore:
    """Thread-safe: the engine writes, the app reads. Like :class:`~mt5copier.store.
    MappingStore`, a connection inherited across a spawn is reopened, never reused."""

    def __init__(self, path: Path | str = ":memory:", *, keep_days: int = 30) -> None:
        self._path = str(path)
        self._keep_days = keep_days
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._conn: sqlite3.Connection | None = None
        self._pruned = 0.0

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None or os.getpid() != self._pid:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executescript(_SCHEMA)
            have = {r["name"] for r in conn.execute("PRAGMA table_info(steps)")}
            for name, decl in _ADDED_COLUMNS:
                if name not in have:
                    conn.execute(f"ALTER TABLE steps ADD COLUMN {name} {decl}")
            conn.commit()
            self._conn, self._pid = conn, os.getpid()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def keep_days(self) -> int:
        return self._keep_days

    @keep_days.setter
    def keep_days(self, days: int) -> None:
        with self._lock:
            if days < self._keep_days:
                self._pruned = 0.0
            self._keep_days = days

    # --- writing ---------------------------------------------------------------------------

    def record(self, step: Step, *, now: float | None = None) -> None:
        """Write one step. A step the same as the trade's last one on that slave is not
        written again: a trade the master already held is announced each time the copier
        starts watching it, and once says it."""
        now = time.time() if now is None else now
        with self._lock:
            try:
                db = self._db
                last = db.execute(
                    "SELECT kind, outcome, action, slave FROM steps WHERE terminal_id = ? "
                    "AND master_ticket = ? ORDER BY ts DESC, id DESC LIMIT 1",
                    (step.terminal_id, step.master_ticket),
                ).fetchone()
                said = (step.kind, step.outcome, step.action, step.slave)
                if last is not None and tuple(last) == said:
                    return
                db.execute(
                    "INSERT INTO steps (ts, terminal_id, master_ticket, symbol, label, kind, "
                    "outcome, problem, master, slave, lots, action) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        step.ts,
                        step.terminal_id,
                        step.master_ticket,
                        step.symbol,
                        step.label,
                        step.kind,
                        step.outcome,
                        int(step.problem),
                        step.master,
                        step.slave,
                        step.lots,
                        step.action,
                    ),
                )
                db.commit()
                if now - self._pruned >= _PRUNE_EVERY:
                    self._prune(now)
            except sqlite3.Error:
                log.exception("could not write a step to the trade history")

    def note(
        self,
        terminal_id: str,
        master_ticket: int,
        symbol: str,
        label: str,
        action: str,
        detail: str,
        *,
        now: float | None = None,
    ) -> None:
        """A step the copier took by itself, such as a breakeven."""
        ts = time.time() if now is None else now
        step = Step(ts, terminal_id, master_ticket, symbol, label, NOTE, "", False, "", detail)
        self.record(replace(step, action=action), now=ts)

    def _prune(self, now: float) -> None:
        cutoff = now - self._keep_days * 86400.0
        gone = self._db.execute("DELETE FROM steps WHERE ts < ?", (cutoff,)).rowcount
        self._db.commit()
        self._pruned = now
        if gone:
            log.info("trade history: deleted %d steps older than %d days", gone, self._keep_days)

    def prune(self, *, now: float | None = None) -> None:
        with self._lock:
            try:
                self._prune(time.time() if now is None else now)
            except sqlite3.Error:
                log.exception("could not prune the trade history")

    # --- reading ---------------------------------------------------------------------------

    def trades(
        self, terminal_id: str, *, since: float | None = None, limit: int = 500
    ) -> list[Trade]:
        """The trades copied to the slave with a step since ``since`` (default: everything
        kept), the most recently copied first, each with every step it has, oldest first."""
        with self._lock:
            try:
                db = self._db
                tickets = [
                    r[0]
                    for r in db.execute(
                        "SELECT master_ticket FROM steps WHERE terminal_id = ? AND ts >= ? "
                        "AND kind IN (?, ?) AND outcome IN (?, ?) "
                        "GROUP BY master_ticket ORDER BY MIN(ts) DESC LIMIT ?",
                        (terminal_id, since or 0.0, *_ENTRIES, *_TAKEN, limit),
                    )
                ]
                if not tickets:
                    return []
                marks = ", ".join("?" * len(tickets))
                rows = db.execute(
                    f"SELECT * FROM steps WHERE terminal_id = ? AND master_ticket IN ({marks}) "
                    "ORDER BY ts, id",
                    (terminal_id, *tickets),
                ).fetchall()
            except sqlite3.Error:
                log.exception("could not read the trade history")
                return []
        return _group(rows, tickets)


def _group(rows: Iterable[sqlite3.Row], order: list[int]) -> list[Trade]:
    steps: dict[int, list[Step]] = {}
    for r in rows:
        steps.setdefault(r["master_ticket"], []).append(
            Step(
                ts=r["ts"],
                terminal_id=r["terminal_id"],
                master_ticket=r["master_ticket"],
                symbol=r["symbol"],
                label=r["label"],
                kind=r["kind"],
                outcome=r["outcome"],
                problem=bool(r["problem"]),
                master=r["master"],
                slave=r["slave"],
                lots=r["lots"],
                action=r["action"],
            )
        )
    out: list[Trade] = []
    for ticket in order:
        got = steps.get(ticket)
        if not got:
            continue
        # The trade's name comes from a step that saw the master's own record, not a note.
        named = next((s for s in got if s.kind != NOTE), got[0])
        out.append(Trade(got[0].terminal_id, ticket, named.symbol, named.label, tuple(got)))
    return out
