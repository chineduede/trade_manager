"""Filesystem layout, resolved correctly whether running from source or a frozen exe.

Centralised deliberately: getting this wrong is what makes a PyInstaller build "work on my
machine" and then silently write its data into whatever directory Explorer happened to supply
as the CWD. Nothing else in the package may build paths from relative strings.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "MT5Copier"

DEFAULT_PROFILE = "default"
"""The profile the data layout started with: the root of :func:`base_data_dir` itself.

Named rather than special-cased everywhere so the picker, the ``--profile`` flag and the lock
files all call it one thing. Its data deliberately stays where it has always been -- a person
who never touches profiles sees no migration and no new folder."""

_DATA_DIR_ENV = "MT5COPIER_DATA_DIR"
"""Hard override of :func:`base_data_dir`, for the rare run that must use a specific root --
pointing a source checkout at the installed app's real data on purpose, or keeping a scratch
root for an experiment. Highest priority: when set, portable markers and the dev split below
are both ignored."""

_PROFILE_ENV = "MT5COPIER_PROFILE"
"""How the chosen profile travels: an environment variable, not a module global.

Deliberate, because workers are *spawn*-started processes that re-import everything -- a
global set in the parent would silently reset to the default in every child, and the child
would write its logs and state into the wrong profile. The environment is inherited through
``spawn``, so setting it once in the entry point covers every process after it."""


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def exe_dir() -> Path:
    """Directory containing the running exe (frozen) or the project root (source)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def base_data_dir() -> Path:
    r"""The app's one writable root, shared by every profile.

    The installed app uses ``%APPDATA%\MT5Copier``. Everything else is kept away from it,
    so development can never spoil the data of the app a person actually uses:

    - ``MT5COPIER_DATA_DIR`` set -> exactly that directory, no questions asked.
    - A ``portable.txt`` marker next to the exe -> an exe-adjacent ``data/`` folder (USB
      stick use, and what the build script plants in ``dist/`` so a freshly built exe
      smoke-tests against its own sandbox -- the installer ships the exe alone, never the
      marker, so an installed copy is unaffected).
    - Running from source (not frozen) -> ``%APPDATA%\MT5Copier-Dev``.

    One consequence to know: the cross-profile claim locks live under this root, so a dev
    instance and the installed app cannot see each other's claims. Do not point both at the
    same terminals at once.
    """
    override = os.environ.get(_DATA_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if (exe_dir() / "portable.txt").exists():
        return exe_dir() / "data"
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / (APP_NAME if is_frozen() else f"{APP_NAME}-Dev")


def active_profile() -> str:
    """The profile this process belongs to. Never empty: no choice means the default."""
    name = os.environ.get(_PROFILE_ENV, "").strip()
    return name or DEFAULT_PROFILE


def set_active_profile(name: str) -> None:
    """Pick the profile for this process and every process it spawns.

    Called exactly once, by the entry point, before anything derives a path. Everything else
    treats the choice as read-only.
    """
    os.environ[_PROFILE_ENV] = name.strip() or DEFAULT_PROFILE


def profiles_root() -> Path:
    """Where the non-default profiles live, one directory per profile."""
    return base_data_dir() / "profiles"


def claims_dir() -> Path:
    """Cross-profile claim locks on terminals and accounts -- see :mod:`mt5copier.claims`.

    Under the *base* directory on purpose: the whole point is that every profile sees the
    same locks, so two instances can never attach the same terminal or copy to the same
    account."""
    return base_data_dir() / "claims"


def shared_dir() -> Path:
    """What every profile reads and writes together -- see :mod:`mt5copier.shared_data`.

    Profiles keep *copying* apart: terminals, roles, settings, books. What describes the
    account itself (its name, its guard rules and their memory) or the machine (the cTrader
    login, the news calendar) is true no matter which profile is looking, so it lives here.
    A folder of its own because the default profile's data dir *is* the base dir -- a shared
    file at the root would be indistinguishable from one of the default profile's own.
    """
    return base_data_dir() / "shared"


def user_data_dir() -> Path:
    r"""Writable directory for the active profile's config, logs and state.

    The default profile is :func:`base_data_dir` itself -- the layout from before profiles
    existed -- and every other profile is a directory of its own under ``profiles/``. Nothing
    else in the package may care which profile is active; it all goes through here.
    """
    name = active_profile()
    if name == DEFAULT_PROFILE:
        return base_data_dir()
    return profiles_root() / name


def config_path() -> Path:
    return user_data_dir() / "config.json"


def log_dir() -> Path:
    return user_data_dir() / "logs"


def lock_path() -> Path:
    """The single-instance lock file -- see :mod:`mt5copier.single_instance`."""
    return user_data_dir() / "mt5copier.lock"


def mapping_db_path() -> Path:
    """SQLite store of which slave position belongs to which master position.

    This is the copier's memory of what it owns. Losing it does not lose money, but it does
    orphan every live copy -- the positions stay open with nothing tracking them -- so it
    lives beside the config rather than anywhere temporary.
    """
    return user_data_dir() / "mapping.db"


def history_db_path() -> Path:
    """SQLite store of each trade's history on each slave, kept for ``history_days``. Display
    only: losing it loses nothing the copier needs."""
    return user_data_dir() / "history.db"


def shared_db_path() -> Path:
    """The one database every profile reads and writes together -- see
    :mod:`mt5copier.shared_db`. It holds the mutable shared stores: account names, guard
    rules and templates, the guards' recorded state, and the prop account containers.
    SQLite rather than the JSON files those once were, so two windows writing in the same
    instant are serialised by the database instead of a hand-rolled folder mutex, and a
    change that touches two stores lands in one transaction."""
    return shared_dir() / "shared.db"


def guards_path() -> Path:
    """Where each account's guard rules and the saved templates lived before they moved
    into :func:`shared_db_path`. Written by the one-time profile gathering and read once by
    the database import; afterwards nothing reads it, but it is the rollback story."""
    return shared_dir() / "guards.json"


def news_path() -> Path:
    """The last news calendar fetched, kept so a restart without the internet still knows it.
    Shared: the week's releases are the same for every profile, so one fetch serves all."""
    return shared_dir() / "news.json"


def ctrader_path() -> Path:
    """The cTrader Open API app's id and secret, the login's tokens, and the accounts it can
    reach. Apart from ``config.json`` because it holds secrets: config is meant to be safe to
    copy or show to someone. Shared: the login belongs to the person's cTrader ID and is
    DPAPI-bound to the Windows user, not to a profile -- one OAuth serves every window."""
    return shared_dir() / "ctrader.json"


def guard_state_path() -> Path:
    """Where the guards' recorded state (day-start equity, held breaches, trade counts)
    lived before it moved into :func:`shared_db_path`. Kept for the one-time import, as
    :func:`guards_path` is."""
    return shared_dir() / "guard_state.json"


def account_names_path() -> Path:
    """Where the friendly name per account (``login@server``) lived before it moved into
    :func:`shared_db_path`. Kept for the one-time import, as :func:`guards_path` is; the
    copy each profile's ``config.json`` carries remains a cache the database overlays."""
    return shared_dir() / "account_names.json"


def catalog_path() -> Path:
    """The symbol catalog: which asset class a symbol is in, for the guard rules that treat
    forex differently from indices. Shared: US30 is an index no matter which profile asks."""
    return shared_dir() / "catalog.json"


def prop_accounts_path() -> Path:
    """Where the prop account containers lived before they moved into
    :func:`shared_db_path`. Kept for the one-time import, as :func:`guards_path` is."""
    return shared_dir() / "prop_accounts.json"


def machine_path() -> Path:
    """The connection timings -- poll, stale, retry, rescan -- shared by every profile: they
    describe this machine and its terminals, not a trading policy."""
    return shared_dir() / "machine.json"


def dryrun_store_path() -> Path:
    """Believed copies for a dry run -- simulated, and kept well away from the real thing.

    A separate file from :func:`mapping_db_path` on purpose. A simulated position recorded in
    the live table would later be indistinguishable from one that actually exists, and the
    copier would try to close something that was never opened.
    """
    return user_data_dir() / "dryrun.db"


def virtual_book_path() -> Path:
    """The virtual master's book, in a profile that trades directly: its open positions,
    resting orders and ticket counter. Losing it loses the master side of every live copy --
    the copies would read as orphans on the next start -- so it lives beside ``mapping.db``
    rather than anywhere temporary."""
    return user_data_dir() / "virtual_master.json"


def version_seen_path() -> Path:
    """The last version whose What's new was read -- see :mod:`mt5copier.release_notes`.

    Install-wide under the base dir on purpose: an update is news exactly once, however many
    profiles run, and dismissing the notes in any window answers for them all."""
    return base_data_dir() / "version_seen.json"


def mt5_terminal_root() -> Path:
    r"""``%APPDATA%\MetaQuotes\Terminal`` — where MT5 keeps one data folder per install."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "MetaQuotes" / "Terminal"


def ensure_dirs() -> None:
    user_data_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    shared_dir().mkdir(parents=True, exist_ok=True)
