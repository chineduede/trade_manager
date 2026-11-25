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


def test_the_account_key_is_the_account_not_the_terminal() -> None:
    mt5 = TerminalConfig(terminal_id="install-1", last_login=777, last_server="FTMO-Server")
    assert ownership.key_of(mt5) == KEY
    assert ownership.key_of(TerminalConfig(terminal_id="fresh")) is None
    ct = TerminalConfig(terminal_id="ct-7", ctrader_account_id=7)
    assert ownership.key_of(ct) == "ctrader:7"


def test_a_terminal_stakes_out_its_install_and_its_account() -> None:
    mt5 = TerminalConfig(terminal_id="install-1", last_login=777, last_server="FTMO-Server")
    assert ownership.keys_of(mt5) == ("terminal:install-1", KEY)
    assert ownership.keys_of(TerminalConfig(terminal_id="fresh")) == ("terminal:fresh",), (
        "never logged in: the install is still ownable at assignment"
    )
    ct = TerminalConfig(terminal_id="ct-7", ctrader_account_id=7)
    assert ownership.keys_of(ct) == ("ctrader:7",), (
        "a cTrader row has no install to fight over; its account is its whole identity"
    )


def test_taking_is_all_or_nothing_across_the_terminals_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account owned elsewhere refuses the take without the install being taken
    either -- a half-taken terminal would block the rightful owner while still
    refusing us."""
    ownership.take(KEY)
    _other_profile(monkeypatch, "scalp")
    mt5 = TerminalConfig(terminal_id="install-1", last_login=777, last_server="FTMO-Server")
    refused = ownership.take_for(mt5)
    assert refused == "the account is owned by profile 'funded'"
    assert ownership.owner("terminal:install-1") is None


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
    assert refused is not None and "assigned by profile 'funded'" in refused, (
        "the install key is checked first: the whole terminal is theirs, not just the account"
    )


def test_assigning_owns_the_install_even_under_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screenshot case: profile A assigns the install while logged into one account;
    profile B's entry for the same install remembers a different account. B may not
    assign it, nor switch it -- the install itself is A's."""
    cfg = _cfg(tmp_path)
    assert role_actions.set_master(cfg, "t-m") is None
    assert ownership.owner("terminal:t-m") == "funded"

    _other_profile(monkeypatch, "scalp")
    exe = tmp_path / "terminal64.exe"
    stale = AppConfig(
        terminals={
            "t-m": TerminalConfig(
                terminal_id="t-m", exe_path=exe, last_login=999, last_server="Other-Server"
            )
        }
    )
    refused = role_actions.set_master(stale, "t-m")
    assert refused is not None and "assigned by profile 'funded'" in refused
    assert ownership.assigned_elsewhere(stale.terminals["t-m"]) == "funded"
    assert ownership.owner("999@Other-Server") is None, "the refusal took nothing"


def test_assigned_elsewhere_is_narrower_than_owned_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign *account* alone still allows the terminal to switch to another account
    -- the documented escape -- so the switch gate asks only about the install."""
    ownership.take(KEY)
    _other_profile(monkeypatch, "scalp")
    mt5 = TerminalConfig(terminal_id="install-1", last_login=777, last_server="FTMO-Server")
    assert ownership.owned_elsewhere_for(mt5) == "funded"
    assert ownership.assigned_elsewhere(mt5) is None


def test_prune_releases_only_our_stale_unbacked_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The janitor's half: rows nothing here uses any more are let go -- but never
    another profile's rows, never a kept key, and never a row too fresh to judge
    (it may be a takeover whose login is still running)."""
    ownership.take(KEY)
    _other_profile(monkeypatch, "scalp")
    ownership.take("888@E8-Demo")

    ownership.prune(set())
    assert ownership.owner("888@E8-Demo") == "scalp", "fresh rows are age-guarded"

    ownership.prune({"888@E8-Demo"}, older_than=0.0)
    assert ownership.owner("888@E8-Demo") == "scalp", "a kept key stays, whatever its age"

    ownership.prune(set(), older_than=0.0)
    assert ownership.owner("888@E8-Demo") is None, "stale and unbacked: released"
    assert ownership.owner(KEY) == "funded", "another profile's row is never ours to prune"


def test_unassigning_releases_the_master_and_every_swept_slave(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    role_actions.set_master(cfg, "t-m")
    role_actions.set_slave(cfg, "t-s")
    swept = role_actions.unassign(cfg, "t-m")
    assert swept == ("t-s",)
    assert ownership.owner("111@FTMO-Server") is None
    assert ownership.owner("222@FTMO-Server") is None
    assert ownership.owner("terminal:t-m") is None
    assert ownership.owner("terminal:t-s") is None
    assert cfg.terminals["t-s"].role is Role.UNASSIGNED
