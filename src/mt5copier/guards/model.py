"""``guards.json``: which rules each account is held to, and the person's saved templates.

Keyed by account (``login@server``, as account names are), not by terminal: a limit such as an
initial balance belongs to one prop account, and a terminal logged into a new challenge starts
with none rather than inheriting the last one's.

A rule is kept as its type name and the parameters as given. A rule this version does not know
-- written by a newer one -- is kept and shown as unknown, never dropped: dropping it would
quietly delete a limit the person set.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..json_store import quarantine, write_json
from ..paths import guards_path
from .base import REGISTRY
from .params import ParamValue, resolve

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


def load(path: Path | None = None) -> GuardsDoc:
    """The guards file, or an empty one if it is absent. A corrupt file is moved aside, as the
    config is, so a bad write never stops the app starting."""
    target = path or guards_path()
    if not target.exists():
        return GuardsDoc()
    try:
        return GuardsDoc.model_validate(json.loads(target.read_text(encoding="utf-8")))
    except Exception as exc:
        quarantine(target, str(exc), "guards")
        return GuardsDoc()


def save(doc: GuardsDoc, path: Path | None = None) -> Path:
    """Write atomically: a temp file beside it, then ``os.replace``.

    The file is shared by every profile, so first fold in what another profile's window may
    have saved since this one loaded: per account and per template, memory wins and keys only
    on disk are adopted into ``doc`` -- nothing in the app ever deletes an account or a
    template, so adopting a disk-only key can never resurrect something just removed. The
    adoption mutates ``doc`` on purpose: the UI and the engine hold it, and should see an
    account another profile has started guarding. Read and write sit under the shared-folder
    lock, so two windows saving in the same instant fold into each other rather than the
    second dropping the first.
    """
    from .. import shared_data

    target = path or guards_path()
    with shared_data.shared_lock():
        if target.exists():
            try:
                disk = GuardsDoc.model_validate(json.loads(target.read_text(encoding="utf-8")))
            except Exception:
                # Unreadable disk copy: write ours whole; load() moves bad files aside anyway.
                disk = None
            if disk is not None:
                for key, guards in disk.accounts.items():
                    doc.accounts.setdefault(key, guards)
                for name, rules in disk.templates.items():
                    doc.templates.setdefault(name, rules)
        write_json(target, doc.model_dump(mode="json"))
    return target
