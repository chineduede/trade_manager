"""``setups.json``: saved setups, each one a master, its slaves, the account each was logged
into, each one's settings, whether copying was on and whether it was a dry run.

Loading one puts all of that back as it was saved. Each account's guards stay with the account,
and come along because the account does. Terminals are named by ``terminal_id``, as the config
keys them, so a setup survives a terminal being moved or renamed. An account is kept by its
login alone: the server is found again, when it is needed, from the accounts the terminal has
(:func:`account_switch.known_accounts`).

**Two settings only ever go the safe way.** A setup can turn a terminal's live-trading override
off but never on, since turning it on takes the terminal's name typed out; and it can go to a
dry run freely but to live copying only with ``LIVE`` typed, as the copier's own settings ask
(see :meth:`Engine.load_setup`).

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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import account_switch
from .account_switch import Account
from .config import AppConfig, Role, TerminalConfig
from .guards.model import write_json
from .logging_setup import get_logger
from .paths import setups_path
from .settings_edit import BY_NAME

log = get_logger(__name__)

SETUPS_VERSION = 1

SAVED_SETTINGS: tuple[str, ...] = (*BY_NAME, "paused", "allow_live_trading")
"""Each terminal's settings a setup keeps: everything its settings forms edit, whether it is
paused, and its live-trading override. Not what describes the terminal itself -- its id, path,
label and accounts -- which is the terminal's, not the setup's."""

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
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Each terminal's :data:`SAVED_SETTINGS` when it was saved, by ``terminal_id``, as JSON. A
    setup saved before settings were kept has none, and loading it leaves them as they are."""
    dry_run: bool | None = None
    """Whether the copier was in a dry run when it was saved. None for a setup saved before
    that was kept: loading it leaves the mode as it is."""

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
        settings={tid: settings_of(cfg.terminals[tid]) for tid in (master.terminal_id, *slaves)},
        dry_run=cfg.dry_run,
    )


def settings_of(terminal: TerminalConfig) -> dict[str, Any]:
    """The settings of one terminal a setup keeps, as JSON."""
    return terminal.model_dump(mode="json", include=set(SAVED_SETTINGS))


def _restored(saved: Mapping[str, Any], current: TerminalConfig) -> dict[str, Any]:
    """What loading puts back on one terminal: the saved settings this version still has,
    with the live-trading override never turned on."""
    wanted = {k: v for k, v in saved.items() if k in SAVED_SETTINGS}
    if wanted.get("allow_live_trading") and not current.allow_live_trading:
        wanted["allow_live_trading"] = False
    return wanted


def held_back(setup: Setup, cfg: AppConfig) -> list[str]:
    """The terminals the setup was saved with live trading allowed on, where it is not now: it
    stays off, and the person turns it on the usual way."""
    return [
        tid
        for tid, saved in setup.settings.items()
        if saved.get("allow_live_trading")
        and tid in cfg.terminals
        and not cfg.terminals[tid].allow_live_trading
    ]


def goes_live(setup: Setup, cfg: AppConfig) -> bool:
    """Whether loading it turns a dry run into live copying, which takes ``LIVE`` typed."""
    return setup.dry_run is False and cfg.dry_run


def restore_settings(setup: Setup, cfg: AppConfig) -> None:
    """Put back each terminal's saved settings, and the dry run or live mode. A terminal whose
    saved settings no longer validate keeps its own, which is said. The caller has checked
    :func:`goes_live`."""
    for tid, saved in setup.settings.items():
        current = cfg.terminals.get(tid)
        if current is None:
            continue
        try:
            new = TerminalConfig.model_validate(
                {**current.model_dump(), **_restored(saved, current)}
            )
        except ValidationError as exc:
            log.warning(
                "%s keeps its own settings: the setup's are not valid here (%s)",
                cfg.display_name(tid),
                exc.error_count(),
            )
            continue
        if new != current:
            cfg.replace_terminal(new)
    if setup.dry_run is not None:
        cfg.dry_run = setup.dry_run


def in_place(setup: Setup, cfg: AppConfig, logins: Logins | None = None) -> bool:
    """Whether everything the setup would put back is already so: roles and accounts, copying,
    the mode and every terminal's settings (as loading would leave them)."""
    now = capture(cfg, logins)
    if now is None or not setup.same_roles(now) or now.copying != setup.copying:
        return False
    if setup.dry_run is not None and setup.dry_run != cfg.dry_run:
        return False
    return not changed_settings(setup, cfg)


def changed_settings(setup: Setup, cfg: AppConfig) -> list[str]:
    """The terminals whose settings now differ from what loading the setup would give them."""
    found = []
    for tid, saved in setup.settings.items():
        current = cfg.terminals.get(tid)
        if current is None:
            continue
        now = settings_of(current)
        if any(now.get(k) != v for k, v in _restored(saved, current).items()):
            found.append(tid)
    return found


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
