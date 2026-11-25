"""Account ownership: one profile owns an account, persistently, everyone else reads.

The multi-profile model's one rule about accounts, replacing the runtime account claims:
using an account takes it, closing the window keeps it, only unassigning, hiding or a
deliberate takeover lets it go. The module is small on purpose; these pin its contract
and the role actions' take/release, which every richer flow builds on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier import ownership, role_actions
from mt5copier.config import AppConfig, Role, TerminalConfig


@pytest.fixture(autouse=True)
def _profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5COPIER_PROFILE", "funded")


def _other_profile(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("MT5COPIER_PROFILE", name)


KEY = "777@FTMO-Server"


def test_taking_is_idempotent_and_refused_to_the_second_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ownership.take(KEY) is None
    assert ownership.take(KEY) is None, "taking what is already ours is a no-op"
    assert ownership.owner(KEY) == "funded"
    assert ownership.owned_elsewhere(KEY) is None, "ours is not elsewhere"

    _other_profile(monkeypatch, "scalp")
    refused = ownership.take(KEY)
    assert refused == "the account is owned by profile 'funded'"
    assert ownership.owned_elsewhere(KEY) == "funded"


def test_release_only_lets_go_of_what_is_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    ownership.take(KEY)
    _other_profile(monkeypatch, "scalp")
    ownership.release(KEY)  # not ours: a no-op, never an overreach
    assert ownership.owner(KEY) == "funded"
    ownership.release(None)  # nothing to do, never an error

    _other_profile(monkeypatch, "funded")
    ownership.release(KEY)
    assert ownership.owner(KEY) is None, "released: free for anyone"
    _other_profile(monkeypatch, "scalp")
    assert ownership.take(KEY) is None


def test_force_take_names_the_loser_and_a_profile_delete_frees_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership.take(KEY)
    _other_profile(monkeypatch, "scalp")
    assert ownership.force_take(KEY) == "funded"
    assert ownership.owner(KEY) == "scalp"
    assert ownership.force_take(KEY) is None, "taking over what is ours loses nobody"

    ownership.take("888@E8-Demo")
    ownership.release_all_of("scalp")
    assert ownership.owner(KEY) is None
    assert ownership.owner("888@E8-Demo") is None


def test_the_ownership_key_is_the_account_not_the_terminal() -> None:
    mt5 = TerminalConfig(terminal_id="install-1", last_login=777, last_server="FTMO-Server")
    assert ownership.key_of(mt5) == KEY
    assert ownership.key_of(TerminalConfig(terminal_id="fresh")) is None
    ct = TerminalConfig(terminal_id="ct-7", ctrader_account_id=7)
    assert ownership.key_of(ct) == "ctrader:7"


def _cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")

    def t(tid: str, login: int) -> TerminalConfig:
        return TerminalConfig(
            terminal_id=tid, exe_path=exe, last_login=login, last_server="FTMO-Server"
        )

    return AppConfig(terminals={"t-m": t("t-m", 111), "t-s": t("t-s", 222)})


def test_assigning_takes_the_account_and_refuses_one_owned_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The double-copy case, now at assignment: one broker account never runs in two
    profiles, however many installs it is logged into."""
    cfg = _cfg(tmp_path)
    assert role_actions.set_master(cfg, "t-m") is None
    assert ownership.owner("111@FTMO-Server") == "funded"
    assert role_actions.set_slave(cfg, "t-s") is None
    assert ownership.owner("222@FTMO-Server") == "funded"

    _other_profile(monkeypatch, "scalp")
    other = _cfg(tmp_path)
    refused = role_actions.set_master(other, "t-m")
    assert refused is not None and "owned by profile 'funded'" in refused


def test_unassigning_releases_the_master_and_every_swept_slave(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    role_actions.set_master(cfg, "t-m")
    role_actions.set_slave(cfg, "t-s")
    swept = role_actions.unassign(cfg, "t-m")
    assert swept == ("t-s",)
    assert ownership.owner("111@FTMO-Server") is None
    assert ownership.owner("222@FTMO-Server") is None
    assert cfg.terminals["t-s"].role is Role.UNASSIGNED
