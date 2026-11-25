"""The desktop account-login flow: the TUI's i key as an ask/commit bridge pair.

Which accounts a terminal knows and what a login does live in mt5copier/account_switch.py and
are pinned by its own tests; these pin the bridge's translation -- who may be logged in, the
rows the pick shows, and what a finished login records.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier import account_switch, ownership, shared_db
from mt5copier.account_switch import Account, LoginResult
from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.desktop.api import DesktopApi

T_FREE = "t-free"
T_SLAVE = "t-slave"
T_CT = "t-ctrader"


class _Engine:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def sync(self) -> None:
        self.calls.append("sync")


def _cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        account_names={"51234@FTMO-Demo": "ftmo eval"},
        terminals={
            T_FREE: TerminalConfig(
                terminal_id=T_FREE,
                label="Spare",
                exe_path=exe,
                role=Role.UNASSIGNED,
                last_login=20481,
                last_server="E8-Demo",
            ),
            T_SLAVE: TerminalConfig(
                terminal_id=T_SLAVE,
                label="FTMO",
                exe_path=exe,
                role=Role.SLAVE,
                last_login=51234,
                last_server="FTMO-Demo",
            ),
            T_CT: TerminalConfig(
                terminal_id=T_CT,
                label="IC cTrader",
                role=Role.UNASSIGNED,
                ctrader_account_id=7,
            ),
        },
    )


_KNOWN = [Account(51234, "FTMO-Demo"), Account(20481, "E8-Demo"), Account(60001, "FTMO-Demo")]


def _api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DesktopApi, _Engine]:
    engine = _Engine()
    api = DesktopApi(cfg=_cfg(tmp_path), engine=engine)  # type: ignore[arg-type]
    monkeypatch.setattr(account_switch, "known_accounts", lambda tid, root=None: list(_KNOWN))
    return api, engine


def test_the_ask_lists_the_saved_accounts_and_marks_the_current_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    out = api.account_login_ask(T_FREE)
    assert out["ok"] is True
    assert out["name"] == "Spare"
    assert out["accounts"] == [
        # The slave's own account is listed but named as held: one terminal per account.
        {
            "key": "51234@FTMO-Demo",
            "words": "ftmo eval · 51234@FTMO-Demo",
            "now": False,
            "usedBy": "ftmo eval",
            "ownedBy": None,
        },
        {
            "key": "20481@E8-Demo",
            "words": "20481@E8-Demo",
            "now": True,
            "usedBy": None,
            "ownedBy": None,
        },
        {
            "key": "60001@FTMO-Demo",
            "words": "60001@FTMO-Demo",
            "now": False,
            "usedBy": None,
            "ownedBy": None,
        },
    ]


def test_only_an_unassigned_mt5_terminal_with_an_exe_may_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    # The slave is named by its account's given name, as every page names it.
    assert "unassign ftmo eval first" in api.account_login_ask(T_SLAVE)["why"]
    assert "cTrader" in api.account_login_ask(T_CT)["why"]
    assert "gone" in api.account_login_ask("t-nothing")["why"]

    monkeypatch.setattr(account_switch, "known_accounts", lambda tid, root=None: [])
    assert "no accounts found for Spare" in api.account_login_ask(T_FREE)["why"]


def test_the_card_offers_a_login_only_when_there_is_a_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    cfg = api._cfg
    assert api._can_switch_account(cfg.terminals[T_FREE]) is True
    assert api._can_switch_account(cfg.terminals[T_SLAVE]) is False, "assigned"
    assert api._can_switch_account(cfg.terminals[T_CT]) is False, "cTrader"

    # One saved account, the one it is on: nothing to pick, so no option.
    api._switchable.clear()
    monkeypatch.setattr(
        account_switch, "known_accounts", lambda tid, root=None: [Account(20481, "E8-Demo")]
    )
    assert api._can_switch_account(cfg.terminals[T_FREE]) is False

    # One saved account but the terminal is not on it: logging in is the pick.
    api._switchable.clear()
    cfg.replace_terminal(
        cfg.terminals[T_FREE].model_copy(update={"last_login": None, "last_server": None})
    )
    assert api._can_switch_account(cfg.terminals[T_FREE]) is True


def test_a_login_records_the_account_the_terminal_landed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, engine = _api(tmp_path, monkeypatch)
    seen: list[tuple[Path, Account]] = []

    def fake_log_in(exe: Path, acct: Account, log_file: Path) -> LoginResult:
        seen.append((exe, acct))
        return LoginResult(True, "logged in", acct.login, acct.server)

    monkeypatch.setattr(account_switch, "log_in", fake_log_in)
    out = api.account_login(T_FREE, "60001@FTMO-Demo")
    assert out["ok"] is True
    assert out["message"] == (
        "Spare is logged in to 60001@FTMO-Demo"
        "; its settings came along from the last account — worth a look"
    )
    assert seen == [(api._cfg.terminals[T_FREE].exe_path, Account(60001, "FTMO-Demo"))]
    tcfg = api._cfg.terminals[T_FREE]
    assert (tcfg.last_login, tcfg.last_server) == (60001, "FTMO-Demo")
    assert "sync" in engine.calls


def test_a_switch_banks_the_old_accounts_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    cfg = api._cfg
    cfg.replace_terminal(cfg.terminals[T_FREE].model_copy(update={"risk_percent": 2.5}))
    monkeypatch.setattr(
        account_switch,
        "log_in",
        lambda exe, acct, log_file: LoginResult(True, "logged in", acct.login, acct.server),
    )
    out = api.account_login(T_FREE, "60001@FTMO-Demo")
    assert out["ok"] is True
    banked = shared_db.get().fetch("account_policies", "20481@E8-Demo")
    assert banked is not None and banked["risk_percent"] == 2.5
    assert cfg.terminals[T_FREE].risk_percent == 2.5, "nothing remembered for the new one yet"


def test_a_switch_back_brings_the_settings_last_used_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    cfg = api._cfg
    db = shared_db.get()
    with db.change(shared_db.POLICIES):
        db.put("account_policies", "60001@FTMO-Demo", {"risk_percent": 0.25, "invert": True})
    monkeypatch.setattr(
        account_switch,
        "log_in",
        lambda exe, acct, log_file: LoginResult(True, "logged in", acct.login, acct.server),
    )
    out = api.account_login(T_FREE, "60001@FTMO-Demo")
    assert out["ok"] is True
    assert out["message"].endswith("; the settings last used on this account are back")
    tcfg = cfg.terminals[T_FREE]
    assert (tcfg.risk_percent, tcfg.invert) == (0.25, True)


def test_an_account_another_terminal_holds_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, engine = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(
        account_switch,
        "log_in",
        lambda exe, acct, log_file: (_ for _ in ()).throw(AssertionError("must not log in")),
    )
    out = api.account_login(T_FREE, "51234@FTMO-Demo")
    assert out["ok"] is False
    assert out["message"] == (
        "ftmo eval (51234@FTMO-Demo) is in use by ftmo eval: "
        "an account is used on one terminal at a time"
    )
    assert engine.calls == []


def test_a_failed_login_changes_nothing_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, engine = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(
        account_switch,
        "log_in",
        lambda exe, acct, log_file: LoginResult(False, "is its password saved in the terminal?"),
    )
    out = api.account_login(T_FREE, "60001@FTMO-Demo")
    assert out["ok"] is False
    assert "Spare was not logged in to 60001@FTMO-Demo" in out["message"]
    tcfg = api._cfg.terminals[T_FREE]
    assert (tcfg.last_login, tcfg.last_server) == (20481, "E8-Demo")
    assert engine.calls == []

    # Already on it: nothing to do, and that is said.
    already = api.account_login(T_FREE, "20481@E8-Demo")
    assert already["ok"] is False
    assert "already logged in" in already["message"]


def test_an_account_owned_elsewhere_refuses_the_login_unless_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forceful way through: a pick another profile owns is refused plainly, and
    ``take=True`` -- sent only after the page's confirmation -- takes the account over
    and logs in, in one call. The ask labels the row so the page knows to ask."""
    api, _ = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(
        account_switch,
        "log_in",
        lambda exe, acct, log_file: LoginResult(True, "logged in", acct.login, acct.server),
    )
    monkeypatch.setenv("MT5COPIER_PROFILE", "scalp")
    ownership.take("60001@FTMO-Demo")
    monkeypatch.delenv("MT5COPIER_PROFILE")

    row = next(
        r for r in api.account_login_ask(T_FREE)["accounts"] if r["key"] == "60001@FTMO-Demo"
    )
    assert row["ownedBy"] == "scalp"

    out = api.account_login(T_FREE, "60001@FTMO-Demo")
    assert out["ok"] is False
    assert "owned by profile 'scalp'" in out["message"]
    assert "take ownership" in out["message"]

    out = api.account_login(T_FREE, "60001@FTMO-Demo", take=True)
    assert out["ok"] is True
    assert ownership.owner("60001@FTMO-Demo") == "default", "taken with the login"
    tcfg = api._cfg.terminals[T_FREE]
    assert (tcfg.last_login, tcfg.last_server) == (60001, "FTMO-Demo")
