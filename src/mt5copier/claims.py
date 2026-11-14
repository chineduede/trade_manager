"""Cross-profile claims: no two running profiles may hold the same terminal or account.

Profiles isolate *files*; they do nothing about the world outside them. The dangerous overlap
is two instances attaching one MT5 terminal, or copying to one broker account through two
different terminal installs -- double copies, with each instance then fighting to "correct"
the other's positions. So before the engine spawns a worker it claims the terminal's identity
and the account's, and a claim another running profile holds refuses the spawn with a message
naming the holder.

Same locking as :mod:`mt5copier.single_instance`, for the same reason: an OS byte lock dies
with its process, so a crashed instance never leaves a stale claim behind. The claim files
live under the *base* data directory (:func:`mt5copier.paths.claims_dir`), the one place every
profile shares, and the owner's name and PID are written after the locked byte so a refused
instance can read who to blame while the lock is held.
"""

from __future__ import annotations

import hashlib
import json
import msvcrt
import os
import re
from pathlib import Path
from typing import TextIO

from .config import TerminalConfig, account_key
from .logging_setup import get_logger
from .single_instance import open_lockable

log = get_logger(__name__)


class ClaimRefusedError(Exception):
    """Another running profile holds this claim."""

    def __init__(self, key: str, owner_profile: str | None, owner_pid: int | None) -> None:
        self.key = key
        self.owner_profile = owner_profile
        self.owner_pid = owner_pid
        what = "the account" if key.startswith(("account:", "ctrader:")) else "the terminal"
        if owner_profile:
            msg = f"{what} is in use by profile {owner_profile!r}"
        else:
            msg = f"{what} is in use by another instance"
        super().__init__(msg)


def claim_keys(tcfg: TerminalConfig) -> list[str]:
    """The identities one terminal config stakes out: the install, and the account in it.

    Both, deliberately. The terminal alone is not enough -- the same account logged into two
    installs is the double-copy case -- and the account alone is not enough either, because a
    terminal whose login is not yet known must still be claimed before a worker launches it.
    """
    keys = [f"terminal:{tcfg.terminal_id}"]
    if tcfg.ctrader_account_id is not None:
        keys.append(f"ctrader:{tcfg.ctrader_account_id}")
    key = account_key(tcfg.last_login, tcfg.last_server)
    if key is not None:
        keys.append(f"account:{key}")
    return keys


def _claim_file(directory: Path, key: str) -> Path:
    """One file per claim, named to be recognisable in Explorer yet collision-free.

    The readable slug is for a person looking at the folder; the hash is what guarantees two
    distinct keys never share a file, whatever characters a server name throws at us.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:60]
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return directory / f"{slug}-{digest}.lock"


def _read_owner(path: Path) -> tuple[str | None, int | None]:
    try:
        with open(path) as f:
            f.seek(1)
            data = json.loads(f.read() or "{}")
        return data.get("profile"), data.get("pid")
    except (OSError, ValueError):
        return None, None


class ClaimBook:
    """The claims this process holds, acquired as workers spawn and dropped as they stop.

    One per engine. Not thread-safe on its own; the engine only touches it under its lock.
    """

    def __init__(self, directory: Path, profile: str) -> None:
        self._dir = directory
        self._profile = profile
        self._held: dict[str, TextIO] = {}

    def acquire(self, keys: list[str]) -> None:
        """Take every key or none: a partial claim would block the rightful holder's retry
        while still refusing our own spawn. Keys already held here are fine -- a worker
        restart re-claims what it never gave up."""
        taken: list[str] = []
        try:
            for key in keys:
                if key in self._held:
                    continue
                self._acquire_one(key)
                taken.append(key)
        except ClaimRefusedError:
            for key in taken:
                self.release([key])
            raise

    def _acquire_one(self, key: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = _claim_file(self._dir, key)
        f = open_lockable(path)  # held while the claim stands
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            f.close()
            profile, pid = _read_owner(path)
            raise ClaimRefusedError(key, profile, pid) from None
        f.seek(1)
        f.write(json.dumps({"profile": self._profile, "pid": os.getpid(), "key": key}))
        f.truncate()
        f.flush()
        self._held[key] = f

    def release(self, keys: list[str]) -> None:
        """Keys not held are ignored: release mirrors acquire's idempotence."""
        for key in keys:
            f = self._held.pop(key, None)
            if f is None:
                continue
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            f.close()
            try:
                # Tidiness only: a lingering file is just an unlocked file, never a stale
                # claim, so a failed delete (someone else mid-probe) costs nothing.
                os.unlink(_claim_file(self._dir, key))
            except OSError:
                pass

    def release_all(self) -> None:
        self.release(list(self._held))
