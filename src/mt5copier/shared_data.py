"""The shared stores that stay files, and the one-time migration that gathers them all.

Profiles exist to keep *copying* apart: which terminals, what roles, dry or live. But some
of what each profile used to keep describes the account itself or this machine, and is true
no matter which profile is looking: an account's friendly name, its guard rules and the
guards' memory of it, the cTrader login, the week's news calendar, and the connection
timings. Those live under :func:`~mt5copier.paths.shared_dir` -- the mutable multi-writer
stores as tables in :mod:`mt5copier.shared_db`, the rest as files. This module owns the
account names (rows in the database, read and folded into each profile's config here), the
machine timings (``machine.json``, last-write-wins by design: they are one machine's
facts), and :func:`migrate`, which builds the shared folder once from whatever each profile
had; :func:`mt5copier.shared_db.get` then carries the migrated JSON into the database.

**Merging in :func:`migrate` is later-wins per key, judged by file modification time.** The
profile that touched a thing last is the one whose word is kept. Nothing is deleted by a
merge: a name or a rule only present in one profile survives.

**Concurrent windows.** Two profiles run as two processes. The database serialises its own
writers; the shared *files* that are read-modify-written -- the symbol catalog, and the
cTrader token refresh (:func:`mt5copier.ctrader.auth.refresh_if_due`), because a refresh
invalidates the old refresh token -- hold :func:`shared_lock`, one mutex for the whole
folder, so two windows writing in the same instant cannot each read the state from before
the other's edit and drop it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import msvcrt
except ImportError:  # not Windows: the app is, but nothing here needs to crash elsewhere
    msvcrt = None  # type: ignore[assignment]

from . import shared_db
from .json_store import read_json, write_json
from .logging_setup import get_logger
from .paths import (
    account_names_path,
    base_data_dir,
    ctrader_path,
    guard_state_path,
    guards_path,
    machine_path,
    news_path,
    profiles_root,
    shared_dir,
)

if TYPE_CHECKING:
    from .config import AppConfig

log = get_logger(__name__)

MACHINE_FIELDS = (
    "poll_interval_ms",
    "heartbeat_timeout_s",
    "rescan_interval_s",
    "reconnect_backoff_s",
)
"""The :class:`AppConfig` fields that describe the machine rather than a trading policy.
Still fields of each profile's config -- the engine and the settings form read them there --
but ``machine.json`` is the source of truth, overlaid on every load."""

_MARKER = "migrated.json"
"""Written once the shared folder has been built, so the gathering never runs again -- not
even when every shared file it produced was empty and so never written."""

_LOCK_NAME = "lock"
"""The mutex file itself; never holds data, only the byte-range lock."""


@contextmanager
def shared_lock(timeout_s: float = 5.0) -> Iterator[None]:
    """The one mutex for every read-modify-write against the shared *files* -- the symbol
    catalog and the cTrader tokens; the stores in :mod:`mt5copier.shared_db` are serialised
    by the database itself and never take this.

    One lock for the whole folder rather than one per file: the writes it guards are a few
    milliseconds of read-parse-replace against rare events, so contention is negligible and
    a single lock cannot deadlock. Byte-range locking through ``msvcrt`` conflicts across
    file handles too, so it also serialises two threads of one window, not only two windows.

    Waiting past ``timeout_s`` raises ``TimeoutError`` -- an ``OSError``, so existing
    catches treat it as any other failed write. A timeout means another process is wedged
    while holding it; writing unlocked then would be a silent return to the race this
    exists to close, so failing loudly is the better outcome.
    """
    if msvcrt is None:
        yield
        return
    shared_dir().mkdir(parents=True, exist_ok=True)
    with open(shared_dir() / _LOCK_NAME, "a+b") as fh:
        deadline = time.monotonic() + timeout_s
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"another window has held the shared-folder lock for {timeout_s:.0f}s"
                    ) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def _data_dirs() -> list[Path]:
    """Every profile's data folder: the base dir (the default profile) and each child of
    ``profiles/``. Only migration looks at these; afterwards nothing reads a profile's copy."""
    dirs = [base_data_dir()]
    try:
        dirs += sorted(p for p in profiles_root().iterdir() if p.is_dir())
    except OSError:
        pass
    return dirs


def _by_age(name: str) -> list[Path]:
    """Each profile's copy of one file, oldest first, so a dict built by updating in order
    keeps the most recently saved value for every key."""
    found = [d / name for d in _data_dirs() if (d / name).is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime)


def migrate() -> None:
    """Build the shared folder once, from what every profile had written.

    Runs at each entry point before anything loads from the shared paths. Guarded by a
    marker file, so it is one-time, and by :func:`shared_lock`, so two profiles launching at
    once build it once: the loser waits, finds the marker, and walks away. The profiles' own
    files are left exactly where they were: nothing reads them any more, but they are the
    rollback story.
    """
    marker = shared_dir() / _MARKER
    if marker.exists():
        return
    with shared_lock(timeout_s=30.0):  # generous: first start may gather several profiles
        if marker.exists():  # the other window built it while this one waited
            return
        _migrate_locked(marker)


def _migrate_locked(marker: Path) -> None:
    names: dict[str, str] = {}
    machine: dict[str, object] = {}
    for path in _by_age("config.json"):
        raw = read_json(path)
        if not isinstance(raw, dict):
            continue
        got = raw.get("account_names")
        if isinstance(got, dict):
            names.update(
                {k: v for k, v in got.items() if isinstance(k, str) and isinstance(v, str) and v}
            )
        machine.update({f: raw[f] for f in MACHINE_FIELDS if f in raw})

    accounts: dict[str, Any] = {}
    templates: dict[str, Any] = {}
    for path in _by_age("guards.json"):
        raw = read_json(path)
        if not isinstance(raw, dict):
            continue
        got = raw.get("accounts")
        if isinstance(got, dict):
            accounts.update(got)
        got = raw.get("templates")
        if isinstance(got, dict):
            templates.update(got)

    day_equity: dict[str, Any] = {}
    latched: dict[str, Any] = {}
    for path in _by_age("guard_state.json"):
        raw = read_json(path)
        if not isinstance(raw, dict):
            continue
        got = raw.get("day_equity")
        if isinstance(got, dict):
            day_equity.update(got)
        got = raw.get("latched")
        if isinstance(got, dict):
            latched.update(got)

    shared_dir().mkdir(parents=True, exist_ok=True)
    if names and not account_names_path().exists():
        write_json(account_names_path(), names)
    if machine and not machine_path().exists():
        write_json(machine_path(), machine)
    if (accounts or templates) and not guards_path().exists():
        # version 1 == guards.model.GUARDS_VERSION; a literal, to keep this import-free.
        write_json(guards_path(), {"version": 1, "accounts": accounts, "templates": templates})
    if (day_equity or latched) and not guard_state_path().exists():
        write_json(guard_state_path(), {"day_equity": day_equity, "latched": latched})
    # Whole-file stores: the newest profile's copy is taken as it stands. ctrader.json is a
    # DPAPI blob bound to the Windows user, so a byte copy stays decryptable.
    for name, target in (("ctrader.json", ctrader_path()), ("news.json", news_path())):
        copies = _by_age(name)
        if copies and not target.exists():
            try:
                target.write_bytes(copies[-1].read_bytes())
            except OSError:
                log.exception("could not carry %s into the shared folder", copies[-1])

    write_json(
        marker,
        {
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "merged_from": [str(d) for d in _data_dirs()],
        },
    )
    log.info("shared folder built at %s from %d profile folder(s)", shared_dir(), len(_data_dirs()))


# --- account names --------------------------------------------------------------------------

_names_stamp_seen: int | None = None
"""The names store's stamp as this process last read or wrote it, so a poll can tell a
rename made in another window from nothing at all without loading the rows every tick."""


def load_names() -> dict[str, str] | None:
    """The shared names, or None when the store has never been written -- which is the one
    case the profile's own ``config.json`` copy still counts (it feeds :func:`migrate`)."""
    db = shared_db.get()
    if db.stamp(shared_db.NAMES) == 0:
        return None
    got = db.load("account_names")
    return {k: v for k, v in got.items() if isinstance(v, str)}


def set_name(key: str, name: str | None) -> None:
    """Set (or, given None, clear) one account's name -- one row, so a rename made in
    another profile's window in the same instant can only race over this same account."""
    global _names_stamp_seen
    db = shared_db.get()
    with db.change(shared_db.NAMES):
        if name:
            db.put("account_names", key, name)
        else:
            db.drop("account_names", key)
    _names_stamp_seen = db.stamp(shared_db.NAMES)


def refresh_names(cfg: AppConfig) -> bool:
    """Fold a rename made from another profile's window into this one's config; True when
    anything changed. Cheap enough to call from a poll: one SELECT, rows only on change."""
    global _names_stamp_seen
    db = shared_db.get()
    stamp = db.stamp(shared_db.NAMES)
    if stamp == 0 or stamp == _names_stamp_seen:
        return False
    _names_stamp_seen = stamp
    names = load_names()
    if names is None or names == cfg.account_names:
        return False
    cfg.account_names = names
    return True


# --- machine timings ------------------------------------------------------------------------


def save_machine(cfg: AppConfig) -> None:
    """Write the machine fields as this config now holds them. Called when a settings save
    actually changed one of them -- not on every config save, so a window that has sat open
    while another changed the timings cannot quietly put the old values back."""
    write_json(machine_path(), {f: getattr(cfg, f) for f in MACHINE_FIELDS})


def apply(cfg: AppConfig) -> None:
    """Overlay the shared stores onto a freshly loaded config: the names wholesale (the
    shared file is the source of truth) and the machine timings field by field, validated
    together so a hand-edited ``machine.json`` cannot smuggle in what the form would refuse."""
    global _names_stamp_seen
    names = load_names()
    if names is not None:
        cfg.account_names = names
        _names_stamp_seen = shared_db.get().stamp(shared_db.NAMES)

    machine = read_json(machine_path())
    if not isinstance(machine, dict):
        return
    picked = {f: machine[f] for f in MACHINE_FIELDS if f in machine}
    if not picked:
        return
    try:
        checked = type(cfg).model_validate({**cfg.model_dump(mode="json"), **picked})
    except Exception as exc:
        log.warning(
            "shared timings at %s are invalid (%s); keeping the profile's own", machine_path(), exc
        )
        return
    for field in picked:
        setattr(cfg, field, getattr(checked, field))
