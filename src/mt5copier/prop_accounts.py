"""Prop account containers: stage chains that carry guard rules from login to login.

A prop firm closes the phase-1 account when it is passed and issues a fresh login for phase
2, and again for the funded account -- and every guard rule and initial balance kept under
``login@server`` dies with the old login. A **container** is a named entity that raw
accounts link into, as an ordered chain of stages, newest = current; old stages are kept,
never forgotten, so a cross-stage timeline stays possible later.

The feature is an aliasing layer, not a copy. Rules for a linked account are read and
written under the container's identity -- the key ``prop:<id>`` in the guards store -- so a
new stage is held to *the same rules object* as the old one, not a snapshot of it. What
resolves through the container is deliberately only what :class:`~mt5copier.guards.model.
AccountGuards` holds: the rules and the initial balance. **Guard state never resolves**:
latches, day-start equity and trade counts stay keyed by the raw account, so a phase-1
breach cannot hold back a funded account that never breached. Unlinked accounts are
untouched -- ``rules_key`` returns the raw key unchanged -- and the standing default
stands: a terminal logged into a new challenge starts with no guards; linking into a
container is the explicit opt-in to inherit.

Shared by every profile and instance, like the guards the containers alias: a table of
:mod:`mt5copier.shared_db`, a row per container. Writes re-read the rows inside one
``change``, and readers re-read on the store's stamp, the way the account names poll.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from . import shared_db
from .logging_setup import get_logger
from .shared_db import SharedDb

log = get_logger(__name__)

PROP_PREFIX = "prop:"
"""How a container's identity reads as a guards key. A raw account key is always
``login@server`` -- digits first -- so the two can never collide."""

NAME_MAX = 32
"""As long as an account's friendly name may be, for the same reason: it is shown in the
same narrow places."""


@dataclass(frozen=True, slots=True)
class Container:
    name: str
    stages: tuple[str, ...]
    """Raw account keys (``login@server``) in the order they were linked; the last one is
    the current stage, every earlier one a closed stage kept for the record."""

    @property
    def current(self) -> str | None:
        return self.stages[-1] if self.stages else None


@dataclass(frozen=True, slots=True)
class PropDoc:
    """One reading of the containers, immutable, safe to hand across threads."""

    containers: dict[str, Container]

    def rules_key(self, key: str | None) -> str | None:
        """The identity this account's guard rules live under: its container's, when it is
        linked into one, else the raw key unchanged. The one resolution the whole feature
        hangs on -- everything that reads or writes :class:`AccountGuards` goes through it,
        and nothing that holds guard *state* does."""
        if key is None:
            return None
        found = self.container_of(key)
        if found is None:
            return key
        cid, _ = found
        return PROP_PREFIX + cid

    def container_of(self, key: str) -> tuple[str, Container] | None:
        for cid, c in self.containers.items():
            if key in c.stages:
                return cid, c
        return None


EMPTY = PropDoc(containers={})


def _parse(raw: object) -> PropDoc:
    """Forgiving, like every shared store: a malformed entry is dropped, never fatal --
    another version of the app may have written it, and starting is worth more than it."""
    if not isinstance(raw, dict):
        return EMPTY
    got = raw.get("containers")
    if not isinstance(got, dict):
        return EMPTY
    containers: dict[str, Container] = {}
    for cid, entry in got.items():
        if not isinstance(cid, str) or not isinstance(entry, dict):
            continue
        name = entry.get("name")
        stages = entry.get("stages")
        if not isinstance(name, str) or not name.strip() or not isinstance(stages, list):
            continue
        kept = tuple(dict.fromkeys(s.strip() for s in stages if isinstance(s, str) and s.strip()))
        containers[cid] = Container(name=name.strip(), stages=kept)
    return PropDoc(containers=containers)


def check_name(name: str, doc: PropDoc, but: str | None = None) -> str | None:
    """Why this container name cannot be used, or None. Mirrors the account-name rules:
    short enough for the narrow places it shows in, and no two containers reading alike --
    two chains both called "FTMO 100k" could not be told apart anywhere."""
    name = name.strip()
    if not name:
        return "the name is empty"
    if len(name) > NAME_MAX:
        return f"keep the name to {NAME_MAX} characters"
    for cid, c in doc.containers.items():
        if cid != but and c.name.casefold() == name.casefold():
            return f"a prop account called {c.name!r} already exists"
    return None


class PropStore:
    """The database-backed containers, re-read when another window saves them (told by
    the store's stamp, the way the shared account names poll).

    Every write re-reads the rows inside one ``change`` and applies one mutation to what
    is really there, so two windows editing containers in the same instant fold into each
    other rather than the second dropping the first. Methods return why the change was
    refused, or None when it took.
    """

    def __init__(self, db: SharedDb | None = None) -> None:
        self._db = db if db is not None else shared_db.get()
        self._stamp: int | None = None
        self._cached: PropDoc = EMPTY

    def _read(self) -> PropDoc:
        return _parse({"containers": self._db.load("prop_containers")})

    def current(self) -> PropDoc:
        try:
            stamp = self._db.stamp(shared_db.PROPS)
            if stamp == self._stamp:
                return self._cached
            doc = self._read()
        except sqlite3.Error:
            log.exception("could not read the prop accounts; keeping what is held")
            return self._cached
        self._stamp = stamp
        self._cached = doc
        return doc

    @staticmethod
    def _row(c: Container) -> dict[str, object]:
        return {"name": c.name, "stages": list(c.stages)}

    def create(self, name: str) -> tuple[str | None, str | None]:
        """A new, empty container: ``(id, None)``, or ``(None, why not)``."""
        name = name.strip()
        with self._db.change(shared_db.PROPS):
            doc = self._read()
            why = check_name(name, doc)
            if why is not None:
                return None, why
            cid = uuid.uuid4().hex[:8]
            self._db.put("prop_containers", cid, self._row(Container(name=name, stages=())))
        self._stamp = None
        log.info("prop account %r created (%s)", name, cid)
        return cid, None

    def rename(self, cid: str, name: str) -> str | None:
        name = name.strip()
        with self._db.change(shared_db.PROPS):
            doc = self._read()
            c = doc.containers.get(cid)
            if c is None:
                return "that prop account is gone"
            why = check_name(name, doc, but=cid)
            if why is not None:
                return why
            self._db.put("prop_containers", cid, self._row(Container(name=name, stages=c.stages)))
        self._stamp = None
        return None

    def delete(self, cid: str) -> str | None:
        """Forget a container and its chain. The rules kept under its identity stay in
        the guards store -- nothing in the app ever deletes a guards entry -- but with no
        stage resolving to them they are dormant, exactly like an unlinked account's."""
        with self._db.change(shared_db.PROPS):
            doc = self._read()
            c = doc.containers.get(cid)
            if c is None:
                return "that prop account is gone"
            self._db.drop("prop_containers", cid)
        self._stamp = None
        log.info("prop account %r deleted; %d stage(s) released", c.name, len(c.stages))
        return None

    def link(self, cid: str, key: str) -> str | None:
        """Make ``key`` the container's current stage. One account lives in one chain: a
        key already linked anywhere -- this container's own closed stages included -- is
        refused by name, so a slip cannot quietly fork an account's identity."""
        key = key.strip()
        if not key or key.startswith(PROP_PREFIX):
            return "that is not an account"
        with self._db.change(shared_db.PROPS):
            doc = self._read()
            c = doc.containers.get(cid)
            if c is None:
                return "that prop account is gone"
            held = doc.container_of(key)
            if held is not None:
                _, other = held
                if other is c:
                    stage = other.stages.index(key) + 1
                    return f"already in this chain (stage {stage}); unlink it first to relink"
                return f"already linked into {other.name!r}"
            self._db.put(
                "prop_containers", cid, self._row(Container(name=c.name, stages=(*c.stages, key)))
            )
        self._stamp = None
        log.info("account %s linked into prop account %r as its current stage", key, c.name)
        return None

    def unlink(self, cid: str, key: str) -> str | None:
        """Take one account out of a chain. Its rules identity falls back to the raw key --
        whatever the guards store still holds there -- and the container's chain closes up
        around the gap."""
        with self._db.change(shared_db.PROPS):
            doc = self._read()
            c = doc.containers.get(cid)
            if c is None:
                return "that prop account is gone"
            if key not in c.stages:
                return "that account is not in this chain"
            kept = Container(name=c.name, stages=tuple(s for s in c.stages if s != key))
            self._db.put("prop_containers", cid, self._row(kept))
        self._stamp = None
        log.info("account %s unlinked from prop account %r", key, c.name)
        return None
