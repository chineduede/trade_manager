"""The desktop's profile management: the TUI picker as a dialog behind the profile pill.

What a profile is and what refuses (the default's name, anything running) are
mt5copier/profiles.py's, pinned by its own tests; these pin the bridge's translation --
the rows the dialog draws, the refusals as messages rather than exceptions, and Open
spawning a detached copier process on the picked profile.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from mt5copier import profiles
from mt5copier.desktop.api import DesktopApi
from mt5copier.paths import DEFAULT_PROFILE


class _Engine:
    def sync(self) -> None:
        pass


def _api() -> DesktopApi:
    return DesktopApi(cfg=None, engine=_Engine())  # type: ignore[arg-type]


def test_the_rows_say_which_is_this_window() -> None:
    profiles.create("funded")
    api = _api()
    out = api.profiles_state()
    assert out["current"] == DEFAULT_PROFILE
    rows = {p["name"]: p for p in out["profiles"]}
    assert rows[DEFAULT_PROFILE]["current"] is True
    assert rows[DEFAULT_PROFILE]["isDefault"] is True
    assert rows["funded"]["current"] is False
    assert rows["funded"]["lastOpened"] == "never opened"


def test_create_rename_delete_come_back_as_messages() -> None:
    api = _api()
    assert api.profile_create("funded")["ok"] is True
    assert profiles.exists("funded")

    out = api.profile_create("funded")
    assert out["ok"] is False
    assert "already exists" in out["message"]

    out = api.profile_create("two words")
    assert out["ok"] is False
    assert "letters, digits" in out["message"]

    assert api.profile_rename("funded", "ftmo")["ok"] is True
    assert profiles.exists("ftmo") and not profiles.exists("funded")

    out = api.profile_rename(DEFAULT_PROFILE, "other")
    assert out["ok"] is False
    assert "keeps its name" in out["message"]

    assert api.profile_delete("ftmo")["ok"] is True
    assert not profiles.exists("ftmo")

    out = api.profile_delete(DEFAULT_PROFILE)
    assert out["ok"] is False
    assert "cannot be deleted" in out["message"]


def test_open_spawns_a_detached_copier_on_that_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles.create("funded")
    api = _api()
    spawned: list[list[str]] = []

    def fake_popen(cmd: list[str], **kw: Any) -> None:
        spawned.append(cmd)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    out = api.profile_open("funded")
    assert out["ok"] is True
    (cmd,) = spawned
    assert cmd[-3:] == ["--desktop", "--profile", "funded"]

    # This window's own profile, and one that does not exist, are refused without a spawn.
    out = api.profile_open(DEFAULT_PROFILE)
    assert out["ok"] is False
    assert "this window" in out["message"]
    out = api.profile_open("nope")
    assert out["ok"] is False
    assert "no profile called" in out["message"]
    assert len(spawned) == 1
