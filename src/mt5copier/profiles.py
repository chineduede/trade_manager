"""Profiles: independent copies of the whole app, one data directory each.

A profile is nothing but a directory -- its own ``config.json``, ``mapping.db``, guards,
setups, logs and single-instance lock -- so two running profiles share no file and cannot
race each other. The *default* profile is the base directory itself, the layout from before
profiles existed; the rest live under ``profiles/<name>``.

What keeps two profiles from touching the same *broker* account or terminal is deliberately
not here: that is :mod:`mt5copier.claims`, which locks identities, not directories.

This module is the picker's model: list, create, rename, delete, and say which profiles are
running right now. It never reads a profile's config through :mod:`mt5copier.config` --
validation there grows with the app, and the picker must still list a profile whose config
the current build would refuse, rather than hide it.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .paths import DEFAULT_PROFILE, base_data_dir, profiles_root
from .single_instance import holder_pid

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

_LOCK_FILE = "mt5copier.lock"
"""Kept in step with :func:`mt5copier.paths.lock_path`, which builds the same name off the
active profile. Spelled here too because this module looks at *other* profiles' locks."""


class ProfileError(Exception):
    """A profile operation refused, with a message fit to show the person."""


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    """One row of the picker."""

    name: str
    path: Path
    is_default: bool
    pid: int | None
    """The PID of the instance running this profile right now, else ``None``."""
    master: str
    """The master's label, or ``""`` for a profile with no master assigned."""
    slaves: int
    """Assigned slave terminals, enabled or not."""
    last_opened: float | None
    """When this profile last ran (its lock file's mtime), else ``None`` -- never opened."""
    trading: bool = False
    """Whether this profile trades directly (a virtual master) rather than copying one."""

    @property
    def running(self) -> bool:
        return self.pid is not None


def valid_name(name: str) -> bool:
    """A profile name is a directory name, so it is kept boring on purpose.

    Letters, digits, dash and underscore, up to 32, starting with a letter or digit: nothing
    Windows reserves, nothing that needs quoting after ``--profile``, and no trailing dot or
    space for Explorer to mangle.
    """
    return bool(_NAME_RE.match(name))


def profile_dir(name: str) -> Path:
    """Where this profile's data lives, whether or not it exists yet."""
    if name == DEFAULT_PROFILE:
        return base_data_dir()
    return profiles_root() / name


def exists(name: str) -> bool:
    return name == DEFAULT_PROFILE or profile_dir(name).is_dir()


def _inspect(name: str, path: Path) -> ProfileInfo:
    master = ""
    slaves = 0
    trading = False
    try:
        raw = json.loads((path / "config.json").read_text(encoding="utf-8"))
        trading = raw.get("mode") == "trading"
        for entry in raw.get("terminals", {}).values():
            role = entry.get("role")
            if role == "slave":
                slaves += 1
            elif role == "master":
                master = (
                    entry.get("label")
                    or (f"{entry.get('last_login')}" if entry.get("last_login") else "")
                    or "master"
                )
    except (OSError, ValueError, AttributeError):
        pass  # no config yet, or one this build cannot parse: the row still shows

    lock = path / _LOCK_FILE
    try:
        last_opened: float | None = lock.stat().st_mtime
    except OSError:
        last_opened = None

    return ProfileInfo(
        name=name,
        path=path,
        is_default=name == DEFAULT_PROFILE,
        pid=holder_pid(lock),
        master=master,
        slaves=slaves,
        trading=trading,
        last_opened=last_opened,
    )


def list_profiles() -> list[ProfileInfo]:
    """Every profile, the default first, the rest by name."""
    out = [_inspect(DEFAULT_PROFILE, base_data_dir())]
    root = profiles_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and valid_name(child.name):
                out.append(_inspect(child.name, child))
    return out


def create(name: str) -> Path:
    """Make a new, empty profile and return its directory."""
    if not valid_name(name):
        raise ProfileError(
            "a profile name is letters, digits, - or _ (up to 32, starting with a letter or digit)"
        )
    if name == DEFAULT_PROFILE or profile_dir(name).exists():
        raise ProfileError(f"a profile called {name!r} already exists")
    path = profile_dir(name)
    path.mkdir(parents=True)
    return path


def rename(old: str, new: str) -> None:
    """Rename a profile. Refused for the default and for one that is running."""
    if old == DEFAULT_PROFILE:
        raise ProfileError("the default profile keeps its name")
    if not exists(old):
        raise ProfileError(f"no profile called {old!r}")
    if not valid_name(new):
        raise ProfileError(
            "a profile name is letters, digits, - or _ (up to 32, starting with a letter or digit)"
        )
    if new == DEFAULT_PROFILE or profile_dir(new).exists():
        raise ProfileError(f"a profile called {new!r} already exists")
    pid = holder_pid(profile_dir(old) / _LOCK_FILE)
    if pid is not None:
        raise ProfileError(f"{old!r} is running (pid {pid}); close it first")
    profile_dir(old).rename(profile_dir(new))


def delete(name: str) -> None:
    """Delete a profile and everything in it. Refused for the default and for one running.

    Destructive by design -- the picker confirms before calling this. What it destroys is the
    profile's *records* (its mapping store, history, guards state); the broker accounts and
    their positions are untouched, but any copies the profile still tracked are orphaned.
    """
    if name == DEFAULT_PROFILE:
        raise ProfileError("the default profile cannot be deleted")
    if not exists(name):
        raise ProfileError(f"no profile called {name!r}")
    pid = holder_pid(profile_dir(name) / _LOCK_FILE)
    if pid is not None:
        raise ProfileError(f"{name!r} is running (pid {pid}); close it first")
    shutil.rmtree(profile_dir(name))
