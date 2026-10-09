"""``setups.json``: saved setups, each one a master, its slaves, the account each was logged
into and whether copying was on.

A setup holds **roles and accounts only**. Each terminal's settings stay with the terminal and
each account's guards stay with the account, so loading a setup changes who copies from whom, on
which accounts, and nothing about how. Terminals are named by ``terminal_id``, as the config
keys them, so a setup survives a terminal being moved or renamed. An account is kept by its
login alone: the server is found again, when it is needed, from the accounts the terminal has
(:func:`account_switch.known_accounts`).

Loading one is the engine's (:meth:`Engine.load_setup`), because it can strand open copies the
same way unassigning does. A terminal logged into another account than the one saved is logged
in first, while it is unassigned, as ``i`` does (see :mod:`mt5copier.account_switch`); the app
runs that. This module only keeps the file and answers questions about it.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Collection, Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import account_switch
from .account_switch import Account
from .config import AppConfig, Role
from .guards.model import write_json
from .logging_setup import get_logger
from .paths import setups_path

log = get_logger(__name__)

SETUPS_VERSION = 1

Logins = Mapping[str, int | None]
"""The account each terminal is logged into now, by ``terminal_id``: None where it is not known."""


class Setup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    master: str
    slaves: tuple[str, ...] = ()
    copying: bool = False
    """Whether copying was on when it was saved; loading it puts it back that way."""
    accounts: dict[str, int] = Field(default_factory=dict)
    """The login each terminal was logged into when it was saved, by ``terminal_id``. A terminal
    left out -- its account was not known, or the setup was saved before accounts were kept --
    is loaded on whatever account it is on."""
    saved_at: float = Field(default_factory=time.time)

    @property
    def terminals(self) -> tuple[str, ...]:
        return (self.master, *self.slaves)

    def same_roles(self, other: Setup) -> bool:
        """Whether ``other`` has these roles, on the accounts this one names."""
        return (
            self.master == other.master
            and set(self.slaves) == set(other.slaves)
            and all(other.accounts.get(tid) == login for tid, login in self.accounts.items())
        )


class SetupsDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = SETUPS_VERSION
    setups: dict[str, Setup] = Field(default_factory=dict)


def logins_of(cfg: AppConfig) -> dict[str, int | None]:
    """The account each terminal was last known to be logged into, from the config alone. The
    engine knows better while a terminal's worker is connected (:meth:`Engine.logins`)."""
    return {tid: t.last_login for tid, t in cfg.terminals.items()}


def capture(cfg: AppConfig, logins: Logins | None = None) -> Setup | None:
    """The roles as they are now, on the accounts they are on, or None while there is no
    master: nothing to save."""
    master = cfg.master
    if master is None:
        return None
    logins = logins_of(cfg) if logins is None else logins
    slaves = tuple(t.terminal_id for t in cfg.slaves)
    return Setup(
        master=master.terminal_id,
        slaves=slaves,
        copying=cfg.copying_enabled,
        accounts={
            tid: login
            for tid in (master.terminal_id, *slaves)
            if (login := logins.get(tid)) is not None
        },
    )


def current_name(doc: SetupsDoc, cfg: AppConfig, logins: Logins | None = None) -> str | None:
    """The saved setup whose roles are the ones in place now, on its accounts, if there is one.
    Whether copying is on is left out of it: switching copying off does not make it a different
    setup."""
    now = capture(cfg, logins)
    if now is None:
        return None
    return next((name for name, s in sorted(doc.setups.items()) if s.same_roles(now)), None)


def switches(setup: Setup, logins: Logins) -> dict[str, int]:
    """The terminals loading this setup logs into another account, and the login each goes to:
    those it names that are on another account now, or on one that is not known."""
    return {
        tid: login
        for tid, login in setup.accounts.items()
        if tid in setup.terminals and logins.get(tid) != login
    }


def find_account(cfg: AppConfig, terminal_id: str, login: int) -> Account | str:
    """The account with this login that has connected in this terminal, or why there is none to
    log into. Only the login is saved, so the server is the one it connected on."""
    name = cfg.display_name(terminal_id)
    found = [a for a in account_switch.known_accounts(terminal_id) if a.login == login]
    if not found:
        return f"{name} has no account {login} any more: log in to it once from MT5"
    if len(found) > 1:
        servers = ", ".join(a.server for a in found)
        return f"{name} has account {login} on more than one server ({servers})"
    return found[0]


def problems(setup: Setup, cfg: AppConfig, logins: Logins | None = None) -> list[str]:
    """Why this setup cannot be loaded here, or nothing. A terminal it names may have been
    removed since, or have no executable to connect with -- the same refusal as pressing m or s
    on it -- or not have the account it has to be logged into."""
    found: list[str] = []
    for tid in setup.terminals:
        tcfg = cfg.terminals.get(tid)
        if tcfg is None:
            found.append(f"terminal {tid[:8]} is no longer found")
        elif not tcfg.usable:
            found.append(f"{cfg.display_name(tid)} has no terminal executable")
    if found:
        return found
    logins = logins_of(cfg) if logins is None else logins
    for tid, login in switches(setup, logins).items():
        account = find_account(cfg, tid, login)
        if isinstance(account, str):
            found.append(account)
    return found


def name_of(cfg: AppConfig, terminal_id: str) -> str:
    """A terminal's name, or its id's start if it is no longer known."""
    if terminal_id in cfg.terminals:
        return cfg.display_name(terminal_id)
    return f"{terminal_id[:8]} (gone)"


def losing(setup: Setup, cfg: AppConfig, logins: Logins | None = None) -> list[str]:
    """The slaves whose copies loading this setup gives up: every one if the master changes --
    another terminal, or the same one on another account -- since what they copied came from the
    old one; otherwise those that stop being slaves, or are logged into another account. A slave
    kept under the same master, on the same account, keeps its copies, still managed."""
    master = cfg.master
    current = [t.terminal_id for t in cfg.slaves]
    moving = switches(setup, logins_of(cfg) if logins is None else logins)
    if master is None or master.terminal_id != setup.master or setup.master in moving:
        return current
    return [tid for tid in current if tid not in setup.slaves or tid in moving]


def apply_roles(setup: Setup, cfg: AppConfig, leave_out: Collection[str] = ()) -> None:
    """Put the setup's roles in place: every terminal it does not name is unassigned. A terminal
    whose role does not change is left alone, so a paused slave that stays one stays paused.

    ``leave_out`` are terminals of the setup to leave unassigned for now: those still to be
    logged into their account, or that could not be. Leaving out the master leaves every
    terminal unassigned and copying off, since a slave cannot be one without it.

    The caller has checked :func:`problems` and given up what :func:`losing` names."""
    wanted: dict[str, Role] = {}
    if setup.master not in leave_out:
        wanted = {tid: Role.SLAVE for tid in setup.slaves if tid not in leave_out}
        wanted[setup.master] = Role.MASTER
    # Unassign first, so the one-master rule holds at every step.
    for tid, tcfg in cfg.terminals.items():
        if tid not in wanted and tcfg.role is not Role.UNASSIGNED:
            cfg.set_role(tid, Role.UNASSIGNED)
    for tid, role in wanted.items():
        if cfg.terminals[tid].role is not role:
            cfg.set_role(tid, role)
    cfg.copying_enabled = setup.copying and setup.master in wanted


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
