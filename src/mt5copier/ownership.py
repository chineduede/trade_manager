"""Ownership: the profile that uses an account or a terminal owns it, until it lets go.

This is the multi-profile model's one rule. The moment a profile starts using a terminal
-- assigning it, logging it into an account, or a worker spawning on it -- *everything it
uses* becomes that profile's: the broker account it holds **and the install itself**, each
a row in ``shared.db`` (the ``account_owners`` table), not a runtime lock, so ownership
stays whether or not the window is open. Every other profile sees the account read-only --
its settings, guard rules, name and prop link cannot be edited there, and no terminal
there can be assigned to it or logged into it -- and sees the assigned install as taken:
it cannot be assigned there, nor even switched to a different account, because switching
would physically yank the terminal out from under the profile running it. An edit or a
grab made in the wrong window is impossible rather than merely discouraged.

Ownership ends three ways: the owner unassigns the terminal holding the account, the owner
hides that terminal (hiding is the release valve: role and settings are kept, but the
terminal and its account are freed for other profiles), or another profile deliberately
takes the account over (:func:`force_take`, behind an explicit confirmation in the UI --
the old owner's running worker is stopped when it notices, and its copies stay open,
unmanaged). Deleting a profile releases everything it owned.

This replaced the runtime *account* claims of :mod:`mt5copier.claims` -- a lock that died
with the process could never say "still mine while my window is closed". Terminal claims
remain runtime locks there, because two running processes attaching one MT5 install is a
physical conflict that ends when either process does.
"""

from __future__ import annotations

import time

from . import shared_db
from .config import TerminalConfig, account_key
from .logging_setup import get_logger
from .paths import active_profile

log = get_logger(__name__)

TABLE = "account_owners"


def key_of(tcfg: TerminalConfig) -> str | None:
    """The identity ownership is kept under: the broker account the terminal holds.

    ``login@server`` for MT5, ``ctrader:<id>`` for cTrader -- the same spelling for the
    same account however many terminal entries point at it. None when the terminal has
    never been seen logged in: nothing to own yet.
    """
    if tcfg.ctrader_account_id is not None:
        return f"ctrader:{tcfg.ctrader_account_id}"
    return account_key(tcfg.last_login, tcfg.last_server)


def terminal_key(tcfg: TerminalConfig) -> str | None:
    """The install's own identity, owned alongside whatever account it holds.

    None for a cTrader row: there is no install to fight over, the account key *is* its
    whole identity. The spelling matches :func:`mt5copier.claims.claim_keys` on purpose --
    the same physical thing, one named lock and one persistent owner.
    """
    if tcfg.is_ctrader:
        return None
    return f"terminal:{tcfg.terminal_id}"


def keys_of(tcfg: TerminalConfig) -> tuple[str, ...]:
    """Everything using this terminal stakes out: the install and the account it holds.

    The install is owned even before the account is known -- assigning a never-logged-in
    terminal already makes it this profile's -- and the account joins the moment a login
    is seen.
    """
    return tuple(k for k in (terminal_key(tcfg), key_of(tcfg)) if k is not None)


def take_for(tcfg: TerminalConfig) -> str | None:
    """Own everything this terminal uses -- the install and its account -- or say why
    not. All or nothing: a half-taken terminal would block the rightful owner while
    still refusing us."""
    return _take_keys(keys_of(tcfg))


def owner(key: str) -> str | None:
    """The profile that owns this account, or None when it is free."""
    row = shared_db.get().fetch(TABLE, key)
    if isinstance(row, dict) and isinstance(who := row.get("profile"), str):
        return who
    return None


def owned_elsewhere(key: str | None) -> str | None:
    """The owning profile's name when that is not this profile; None when free or ours."""
    if key is None:
        return None
    who = owner(key)
    return who if who is not None and who != active_profile() else None


def owned_elsewhere_for(tcfg: TerminalConfig) -> str | None:
    """The foreign profile holding *anything* this terminal uses -- its install or its
    account -- or None when every key is free or ours. The one question a card, a drag
    and a settings save all ask."""
    for key in keys_of(tcfg):
        who = owned_elsewhere(key)
        if who is not None:
            return who
    return None


def assigned_elsewhere(tcfg: TerminalConfig) -> str | None:
    """The foreign profile whose assignment holds this *install*, or None.

    Narrower than :func:`owned_elsewhere_for` on purpose: an account owned by a closed
    profile still allows this terminal to be logged into a *different* account -- that is
    the documented escape -- but an install assigned in another profile allows nothing at
    all, switching included.
    """
    return owned_elsewhere(terminal_key(tcfg))


def take(key: str) -> str | None:
    """Own one key, or return why not. Idempotent: taking what is ours is a no-op."""
    return _take_keys((key,))


def _blame(key: str, who: str) -> str:
    if key.startswith("terminal:"):
        return f"the terminal is assigned by profile {who!r}"
    return f"the account is owned by profile {who!r}"


def _take_keys(keys: tuple[str, ...]) -> str | None:
    """Own every key or none, in one :meth:`~mt5copier.shared_db.SharedDb.change`, so two
    profiles racing for the same free terminal cannot both win -- and a key already owned
    elsewhere refuses the lot before anything is written."""
    if not keys:
        return None
    me = active_profile()
    db = shared_db.get()
    with db.change(shared_db.OWNERS):
        rows = {key: db.fetch(TABLE, key) for key in keys}
        for key, row in rows.items():
            who = row.get("profile") if isinstance(row, dict) else None
            if isinstance(who, str) and who != me:
                return _blame(key, who)
        for key, row in rows.items():
            who = row.get("profile") if isinstance(row, dict) else None
            if who != me:
                db.put(TABLE, key, {"profile": me, "taken": time.time()})
                log.info("%s is now owned by profile %r", key, me)
    return None


def release_for(tcfg: TerminalConfig) -> None:
    """Let go of everything this terminal uses -- its install and its account."""
    for key in keys_of(tcfg):
        release(key)


def force_take_for(tcfg: TerminalConfig) -> str | None:
    """Take over everything this terminal uses; returns who lost any of it, if anyone.

    The deliberate escape hatch behind the UI's confirmation, like :func:`force_take`,
    which it calls per key.
    """
    lost: str | None = None
    for key in keys_of(tcfg):
        lost = force_take(key) or lost
    return lost


def release(key: str | None) -> None:
    """Let the account go -- only if it is this profile's. Anything else is a no-op:
    releasing what another profile owns would be the exact overreach ownership exists
    to prevent, and releasing what nobody owns has nothing to do."""
    if key is None:
        return
    me = active_profile()
    db = shared_db.get()
    with db.change(shared_db.OWNERS):
        row = db.fetch(TABLE, key)
        who = row.get("profile") if isinstance(row, dict) else None
        if who == me:
            db.drop(TABLE, key)
            log.info("%s released by profile %r", key, me)


def force_take(key: str) -> str | None:
    """Take the account over regardless of its owner; returns who lost it, if anyone.

    The deliberate escape hatch, called only after the person confirmed: the old owner's
    profile keeps its terminal entry, but its worker is stopped when it notices the loss
    and its copies stay open unmanaged. Never called by machinery on its own.
    """
    me = active_profile()
    db = shared_db.get()
    lost: str | None = None
    with db.change(shared_db.OWNERS):
        row = db.fetch(TABLE, key)
        who = row.get("profile") if isinstance(row, dict) else None
        if isinstance(who, str) and who != me:
            lost = who
        db.put(TABLE, key, {"profile": me, "taken": time.time()})
    log.warning("%s taken over by profile %r (was %r)", key, me, lost)
    return lost


def release_all_of(profile: str) -> None:
    """Drop everything this profile owns -- the profile is being deleted."""
    db = shared_db.get()
    with db.change(shared_db.OWNERS):
        for key, row in db.load(TABLE).items():
            if isinstance(row, dict) and row.get("profile") == profile:
                db.drop(TABLE, key)
