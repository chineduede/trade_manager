"""Profiles: the picker's model. All against the suite's isolated %APPDATA%."""

from __future__ import annotations

import json

import pytest

from mt5copier import profiles
from mt5copier.paths import (
    DEFAULT_PROFILE,
    base_data_dir,
    profiles_root,
    set_active_profile,
    user_data_dir,
)
from mt5copier.single_instance import SingleInstanceLock


def test_the_default_profile_is_the_base_directory_itself() -> None:
    assert profiles.profile_dir(DEFAULT_PROFILE) == base_data_dir()
    assert profiles.exists(DEFAULT_PROFILE), "the default exists even before any file does"


def test_the_active_profile_decides_where_every_path_lands() -> None:
    assert user_data_dir() == base_data_dir()
    set_active_profile("funded")
    try:
        assert user_data_dir() == profiles_root() / "funded"
    finally:
        set_active_profile(DEFAULT_PROFILE)
    assert user_data_dir() == base_data_dir()


@pytest.mark.parametrize("name", ["funded", "group-2", "a", "A_32charsxxxxxxxxxxxxxxxxxxxxxxx"])
def test_sensible_names_are_accepted(name: str) -> None:
    assert profiles.valid_name(name)


@pytest.mark.parametrize("name", ["", " ", "-x", "two words", "dot.", "a/b", "..", "x" * 33])
def test_hostile_names_are_refused(name: str) -> None:
    assert not profiles.valid_name(name)


def test_create_list_rename_delete_round_trip() -> None:
    profiles.create("alpha")
    profiles.create("beta")
    names = [p.name for p in profiles.list_profiles()]
    assert names == [DEFAULT_PROFILE, "alpha", "beta"], "default first, the rest by name"

    profiles.rename("beta", "gamma")
    assert profiles.exists("gamma") and not profiles.exists("beta")

    profiles.delete("alpha")
    assert not profiles.exists("alpha")
    assert not profiles.profile_dir("alpha").exists()


def test_the_default_can_be_neither_renamed_nor_deleted_nor_remade() -> None:
    with pytest.raises(profiles.ProfileError):
        profiles.rename(DEFAULT_PROFILE, "something")
    with pytest.raises(profiles.ProfileError):
        profiles.delete(DEFAULT_PROFILE)
    with pytest.raises(profiles.ProfileError):
        profiles.create(DEFAULT_PROFILE)


def test_creating_a_name_twice_is_refused() -> None:
    profiles.create("alpha")
    with pytest.raises(profiles.ProfileError):
        profiles.create("alpha")


def test_a_running_profile_shows_its_pid_and_refuses_rename_and_delete() -> None:
    path = profiles.create("busy")
    lock = SingleInstanceLock(path / "mt5copier.lock")
    lock.acquire()
    try:
        info = next(p for p in profiles.list_profiles() if p.name == "busy")
        assert info.running
        assert info.pid is not None
        with pytest.raises(profiles.ProfileError, match="running"):
            profiles.rename("busy", "idle")
        with pytest.raises(profiles.ProfileError, match="running"):
            profiles.delete("busy")
    finally:
        lock.release()
    assert not next(p for p in profiles.list_profiles() if p.name == "busy").running


def test_metadata_comes_from_the_profiles_own_config() -> None:
    path = profiles.create("funded")
    (path / "config.json").write_text(
        json.dumps(
            {
                "terminals": {
                    "m": {"role": "master", "label": "FTMO main", "last_login": 111},
                    "s1": {"role": "slave"},
                    "s2": {"role": "slave"},
                    "u": {"role": "unassigned"},
                }
            }
        ),
        encoding="utf-8",
    )
    info = next(p for p in profiles.list_profiles() if p.name == "funded")
    assert info.master == "FTMO main"
    assert info.slaves == 2


def test_a_profile_with_an_unreadable_config_still_lists() -> None:
    """The picker must show a profile this build cannot parse, never hide it."""
    path = profiles.create("odd")
    (path / "config.json").write_text("{not json", encoding="utf-8")
    names = [p.name for p in profiles.list_profiles()]
    assert "odd" in names
