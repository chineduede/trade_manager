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

    Uses ``%APPDATA%\MT5Copier``. A ``portable.txt`` marker next to the exe switches to an
    exe-adjacent ``data/`` folder instead, so the app can live on a USB stick.
    """
    if (exe_dir() / "portable.txt").exists():
        return exe_dir() / "data"
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_NAME


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


def guards_path() -> Path:
    """Each account's guard rules, and the person's saved templates of them. Apart from
    ``config.json`` so that a slip editing rules cannot disturb copy settings. Shared by
    every profile: a rule belongs to the account, and the account is the same account no
    matter which profile's terminal is logged into it."""
    return shared_dir() / "guards.json"


def setups_path() -> Path:
    """The person's saved setups: which terminal is the master, which are slaves, and whether
    copying was on. Apart from ``config.json``, as the guards are."""
    return user_data_dir() / "setups.json"


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
    """What the guards have recorded from earlier snapshots, such as the equity at a reset.
    Shared, as the rules are: a daily-loss anchor or a held breach belongs to the account,
    and must not restart from zero because the account is watched from another profile."""
    return shared_dir() / "guard_state.json"


def account_names_path() -> Path:
    """The friendly name per account (``login@server``), shared by every profile: the same
    account must read as the same name in every window. The source of truth; the copy each
    profile's ``config.json`` still carries is a cache this overlays on load."""
    return shared_dir() / "account_names.json"


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


def mt5_terminal_root() -> Path:
    r"""``%APPDATA%\MetaQuotes\Terminal`` — where MT5 keeps one data folder per install."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "MetaQuotes" / "Terminal"


def ensure_dirs() -> None:
    user_data_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    shared_dir().mkdir(parents=True, exist_ok=True)
