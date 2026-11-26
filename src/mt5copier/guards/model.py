"""The guards store: which rules each account is held to, and the person's saved templates.

Keyed by account (``login@server``, as account names are), not by terminal: a limit such as an
initial balance belongs to one prop account, and a terminal logged into a new challenge starts
with none rather than inheriting the last one's. On disk the store is two tables of
:mod:`mt5copier.shared_db` -- a row per account and a row per template.

A rule is kept as its type name and the parameters as given. A rule this version does not know
-- written by a newer one -- is kept and shown as unknown, never dropped: dropping it would
quietly delete a limit the person set. A whole *row* this version cannot read is skipped on
load and said in the log, but never deleted either: nothing here writes a key it does not
hold, so the row survives in the store for the version that understands it.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .. import shared_db
from ..logging_setup import get_logger
from ..shared_db import SharedDb
from .base import REGISTRY
from .params import ParamValue, resolve

log = get_logger(__name__)

GUARDS_VERSION = 1


class GuardRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    type: str
    enabled: bool = True
    params: dict[str, ParamValue] = Field(default_factory=dict)

    def resolved(self) -> tuple[dict[str, ParamValue], list[str]]:
        """Every parameter's value and any problems, or a problem saying the type is unknown."""
        rule = REGISTRY.get(self.type)
        if rule is None:
            return dict(self.params), [f"no rule called {self.type!r} in this version"]
        return resolve(rule.params, self.params)


class AccountGuards(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    """Switches the whole set off without forgetting it."""
    initial_balance: float | None = Field(default=None, gt=0)
    """The account size, which percent limits are usually taken of."""
    rules: tuple[GuardRule, ...] = ()


class GuardsDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = GUARDS_VERSION
    accounts: dict[str, AccountGuards] = Field(default_factory=dict)
    templates: dict[str, tuple[GuardRule, ...]] = Field(default_factory=dict)
    """Named sets of rules the person saved, to apply to another account."""

    def for_account(self, key: str | None) -> AccountGuards:
        if key is None:
            return AccountGuards()
        return self.accounts.get(key, AccountGuards())

    def set_account(self, key: str, guards: AccountGuards) -> None:
        """One assignment, so the drain thread reads the set from before or after, never half."""
        self.accounts[key] = guards


def _account(value: Any) -> AccountGuards | None:
    try:
        return AccountGuards.model_validate(value)
    except Exception:
        return None


def _rules(value: Any) -> tuple[GuardRule, ...] | None:
    if not isinstance(value, list):
        return None
    try:
        return tuple(GuardRule.model_validate(r) for r in value)
    except Exception:
        return None


def load(db: SharedDb | None = None) -> GuardsDoc:
    """The guards as the shared database holds them, or an empty doc when it holds none.
    A row that cannot be read is skipped and said in the log -- see the module docstring
    for why that never loses it."""
    store = db if db is not None else shared_db.get()
    doc = GuardsDoc()
    for key, value in store.load("guard_accounts").items():
        got = _account(value)
        if got is not None:
            doc.accounts[key] = got
        else:
            log.warning("guards for %s could not be read; left in the store, not shown", key)
    for name, value in store.load("guard_templates").items():
        rules = _rules(value)
        if rules is not None:
            doc.templates[name] = rules
        else:
            log.warning("template %r could not be read; left in the store, not shown", name)
    return doc


def save(doc: GuardsDoc, db: SharedDb | None = None) -> None:
    """Write ``doc`` into the shared database, one transaction.

    The store is shared by every profile, so first fold in what another profile's window
    may have saved since this one loaded: per account and per template, memory wins and
    keys only the store has are adopted into ``doc`` -- nothing in the app ever deletes an
    account, and a template is deleted from memory and store in one transaction (see
    :func:`delete_template`), so adopting a store-only key can never resurrect something
    this window just removed. The adoption mutates ``doc`` on purpose: the UI and the
    engine hold it, and should see an account another profile has started guarding. Read
    and write sit in one ``change``, so two windows saving in the same instant fold into
    each other rather than the second dropping the first.
    """
    store = db if db is not None else shared_db.get()
    with store.change(shared_db.GUARDS):
        _fold_in(doc, store)
        _write(doc, store)


def _fold_in(doc: GuardsDoc, store: SharedDb) -> None:
    """Adopt into ``doc`` the keys only the store has -- what another window saved since
    this one loaded. Memory wins per key. Call inside the ``change``."""
    for key, value in store.load("guard_accounts").items():
        if key not in doc.accounts:
            got = _account(value)
            if got is not None:
                doc.accounts[key] = got
    for name, value in store.load("guard_templates").items():
        if name not in doc.templates:
            rules = _rules(value)
            if rules is not None:
                doc.templates[name] = rules


def _write(doc: GuardsDoc, store: SharedDb) -> None:
    for key, guards in doc.accounts.items():
        store.put("guard_accounts", key, guards.model_dump(mode="json"))
    for name, rules in doc.templates.items():
        store.put("guard_templates", name, [r.model_dump(mode="json") for r in rules])


def delete_template(doc: GuardsDoc, name: str, db: SharedDb | None = None) -> None:
    """Drop one saved template from ``doc`` and the store in one transaction, so a save
    racing from another window folds around the removal rather than past it. A window that
    still holds the name in memory adopts the removal on its next poll; only a guards save
    it makes in that gap writes the template back -- the cost of a store with no
    tombstones, and a far smaller door than leaving the row behind."""
    store = db if db is not None else shared_db.get()
    with store.change(shared_db.GUARDS):
        _fold_in(doc, store)
        doc.templates.pop(name, None)
        store.drop("guard_templates", name)
        _write(doc, store)
