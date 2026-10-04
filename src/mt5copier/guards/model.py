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
import os
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..logging_setup import get_logger
from ..paths import guards_path
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


def load(path: Path | None = None) -> GuardsDoc:
    """The guards file, or an empty one if it is absent. A corrupt file is moved aside, as the
    config is, so a bad write never stops the app starting."""
    target = path or guards_path()
    if not target.exists():
        return GuardsDoc()
    try:
        return GuardsDoc.model_validate(json.loads(target.read_text(encoding="utf-8")))
    except Exception as exc:
        backup = target.with_suffix(f".bad-{os.getpid()}.json")
        try:
            target.replace(backup)
            log.error("guards at %s were invalid (%s); moved to %s", target, exc, backup)
        except OSError:
            log.error("guards at %s were invalid (%s) and could not be moved aside", target, exc)
        return GuardsDoc()


def save(doc: GuardsDoc, path: Path | None = None) -> Path:
    """Write atomically: a temp file beside it, then ``os.replace``."""
    target = path or guards_path()
    write_json(target, doc.model_dump(mode="json"))
    return target


def write_json(target: Path, payload: object) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
