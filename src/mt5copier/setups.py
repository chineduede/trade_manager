"""``setups.json``: saved setups, each one a master, its slaves and whether copying was on.

A setup holds **roles only**. Each terminal's settings stay with the terminal and each account's
guards stay with the account, so loading a setup changes who copies from whom and nothing about
how. Terminals are named by ``terminal_id``, as the config keys them, so a setup survives a
terminal being moved or renamed.

Loading one is the engine's (:meth:`Engine.load_setup`), because it can strand open copies the
same way unassigning does. This module only keeps the file and answers questions about it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .config import AppConfig, Role
from .guards.model import write_json
from .logging_setup import get_logger
from .paths import setups_path

log = get_logger(__name__)

SETUPS_VERSION = 1


class Setup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    master: str
    slaves: tuple[str, ...] = ()
    copying: bool = False
    """Whether copying was on when it was saved; loading it puts it back that way."""
    saved_at: float = Field(default_factory=time.time)

    def same_roles(self, other: Setup) -> bool:
        return self.master == other.master and set(self.slaves) == set(other.slaves)


class SetupsDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = SETUPS_VERSION
    setups: dict[str, Setup] = Field(default_factory=dict)


def capture(cfg: AppConfig) -> Setup | None:
    """The roles as they are now, or None while there is no master: nothing to save."""
    master = cfg.master
    if master is None:
        return None
    return Setup(
        master=master.terminal_id,
        slaves=tuple(t.terminal_id for t in cfg.slaves),
        copying=cfg.copying_enabled,
    )


def current_name(doc: SetupsDoc, cfg: AppConfig) -> str | None:
    """The saved setup whose roles are the ones in place now, if there is one. Whether copying
    is on is left out of it: switching copying off does not make it a different setup."""
    now = capture(cfg)
    if now is None:
        return None
    return next((name for name, s in sorted(doc.setups.items()) if s.same_roles(now)), None)


def problems(setup: Setup, cfg: AppConfig) -> list[str]:
    """Why this setup cannot be loaded here, or nothing. A terminal it names may have been
    removed since, or have no executable to connect with -- the same refusal as pressing m or s
    on it."""
    found: list[str] = []
    for tid in (setup.master, *setup.slaves):
        tcfg = cfg.terminals.get(tid)
        if tcfg is None:
            found.append(f"terminal {tid[:8]} is no longer found")
        elif tcfg.exe_path is None:
            found.append(f"{cfg.display_name(tid)} has no terminal executable")
    return found


def name_of(cfg: AppConfig, terminal_id: str) -> str:
    """A terminal's name, or its id's start if it is no longer known."""
    if terminal_id in cfg.terminals:
        return cfg.display_name(terminal_id)
    return f"{terminal_id[:8]} (gone)"


def losing(setup: Setup, cfg: AppConfig) -> list[str]:
    """The slaves whose copies loading this setup gives up: every one if the master changes,
    since what they copied came from the old one; otherwise those that stop being slaves. A slave
    kept under the same master keeps its copies, still managed."""
    master = cfg.master
    current = [t.terminal_id for t in cfg.slaves]
    if master is None or master.terminal_id != setup.master:
        return current
    return [tid for tid in current if tid not in setup.slaves]


def apply_roles(setup: Setup, cfg: AppConfig) -> None:
    """Put the setup's roles in place: every terminal it does not name is unassigned. A terminal
    whose role does not change is left alone, so a paused slave that stays one stays paused.
    The caller has checked :func:`problems` and given up what :func:`losing` names."""
    wanted = {tid: Role.SLAVE for tid in setup.slaves}
    wanted[setup.master] = Role.MASTER
    # Unassign first, so the one-master rule holds at every step.
    for tid, tcfg in cfg.terminals.items():
        if tid not in wanted and tcfg.role is not Role.UNASSIGNED:
            cfg.set_role(tid, Role.UNASSIGNED)
    for tid, role in wanted.items():
        if cfg.terminals[tid].role is not role:
            cfg.set_role(tid, role)
    cfg.copying_enabled = setup.copying


def load(path: Path | None = None) -> SetupsDoc:
    """The setups file, or an empty one if it is absent. A corrupt file is moved aside, as the
    config is, so a bad write never stops the app starting."""
    target = path or setups_path()
    if not target.exists():
        return SetupsDoc()
    try:
        return SetupsDoc.model_validate(json.loads(target.read_text(encoding="utf-8")))
    except Exception as exc:
        backup = target.with_suffix(f".bad-{os.getpid()}.json")
        try:
            target.replace(backup)
            log.error("setups at %s were invalid (%s); moved to %s", target, exc, backup)
        except OSError:
            log.error("setups at %s were invalid (%s) and could not be moved aside", target, exc)
        return SetupsDoc()


def save(doc: SetupsDoc, path: Path | None = None) -> Path:
    """Write atomically: a temp file beside it, then ``os.replace``."""
    target = path or setups_path()
    write_json(target, doc.model_dump(mode="json"))
    return target
