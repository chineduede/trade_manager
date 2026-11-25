"""Account ownership: the profile that uses an account owns it, until it lets go.

This is the multi-profile model's one rule about accounts. The moment a profile starts
using an account -- assigning a terminal that holds it, logging an assigned terminal into
it, or a worker spawning on it -- the account becomes that profile's, and it *stays* that
profile's whether or not its window is open: ownership is a row in ``shared.db``
(the ``account_owners`` table), not a runtime lock. Every other profile sees the account
read-only -- its settings, guard rules, name and prop link cannot be edited there, and no
terminal there can be assigned to it or logged into it -- so an edit made in the wrong
window is impossible rather than merely discouraged.

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


def take_for(tcfg: TerminalConfig) -> str | None:
    """Take ownership of the account this terminal holds, or say why not. A terminal with
    no known account owns nothing yet, and that is not a refusal."""
    key = key_of(tcfg)
    return take(key) if key is not None else None


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


def take(key: str) -> str | None:
    """Own the account, or return why not. Idempotent: taking what is ours is a no-op.

    The read and the write sit in one :meth:`~mt5copier.shared_db.SharedDb.change`, so two
    profiles racing for a free account cannot both win.
    """
    me = active_profile()
    db = shared_db.get()
    with db.change(shared_db.OWNERS):
        row = db.fetch(TABLE, key)
        who = row.get("profile") if isinstance(row, dict) else None
        if isinstance(who, str) and who != me:
            return f"the account is owned by profile {who!r}"
        if who != me:
            db.put(TABLE, key, {"profile": me, "taken": time.time()})
            log.info("account %s is now owned by profile %r", key, me)
    return None


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
            log.info("account %s released by profile %r", key, me)


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
    log.warning("account %s taken over by profile %r (was %r)", key, me, lost)
    return lost


def release_all_of(profile: str) -> None:
    """Drop every account this profile owns -- the profile is being deleted."""
    db = shared_db.get()
    with db.change(shared_db.OWNERS):
        for key, row in db.load(TABLE).items():
            if isinstance(row, dict) and row.get("profile") == profile:
                db.drop(TABLE, key)
