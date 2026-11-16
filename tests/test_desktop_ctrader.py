"""The desktop's cTrader login: the TUI t flow as a bridge pair.

The OAuth dance itself is mt5copier/ctrader/auth.py's, pinned by its own tests; these pin
the bridge's translation -- the dialog state (which never carries the secret), the
validation that refuses before any browser opens, the blank-secret-keeps-the-saved-one
rule, the config merge with its toast wording, and reconnecting assigned cTrader slaves
so their workers read the new token.
"""

from __future__ import annotations

from typing import Any

import pytest

from mt5copier.config import AppConfig, Role, TerminalConfig, ctrader_terminal_id
from mt5copier.ctrader import auth as ctrader_auth
from mt5copier.ctrader.auth import CTraderAccount, CTraderCredentials, LoginError
from mt5copier.desktop.api import DesktopApi


class _Engine:
    """Only what the cTrader login reaches for, recorded rather than done."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def reconnect(self, tid: str) -> None:
        self.calls.append(("reconnect", tid))

    def sync(self) -> None:
        self.calls.append(("sync", None))


def _api(cfg: AppConfig | None = None) -> tuple[DesktopApi, _Engine, AppConfig]:
    cfg = cfg if cfg is not None else AppConfig()
    engine = _Engine()
    return DesktopApi(cfg=cfg, engine=engine), engine, cfg  # type: ignore[arg-type]


def test_the_dialog_state_never_carries_the_secret() -> None:
    ctrader_auth.save(
        CTraderCredentials(
            client_id="app-id",
            client_secret="shh-secret",
            access_token="tok",
            accounts=[CTraderAccount(account_id=1, is_live=False)],
        )
    )
    api, _, _ = _api()
    s = api.ctrader_login_state()
    assert s["clientId"] == "app-id"
    assert s["hasSecret"] is True
    assert s["loggedIn"] is True
    assert s["accounts"] == 1
    assert s["waiting"] is False
    assert "shh-secret" not in str(s)


def test_validation_refuses_before_any_browser_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    api, _, _ = _api()
    opened: list[int] = []
    monkeypatch.setattr(ctrader_auth, "log_in", lambda *a, **k: opened.append(1))

    out = api.ctrader_login("", "", "http://localhost:9290/callback")
    assert out["ok"] is False
    assert "both needed" in out["message"]

    out = api.ctrader_login("app-id", "shh", "https://example.com/callback")
    assert out["ok"] is False
    assert "redirect" in out["message"].lower()
    assert opened == []


def test_a_blank_secret_keeps_the_saved_one_and_the_accounts_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctrader_auth.save(CTraderCredentials(client_id="app-id", client_secret="shh-secret"))
    api, engine, cfg = _api()
    seen: dict[str, CTraderCredentials] = {}

    def fake_log_in(creds: CTraderCredentials, path: Any = None, cancel: Any = None) -> list:
        seen["creds"] = creds
        return [CTraderAccount(account_id=7, is_live=False, login=111, broker="IC Markets")]

    monkeypatch.setattr(ctrader_auth, "log_in", fake_log_in)
    out = api.ctrader_login("app-id", "", "http://localhost:9290/callback")
    assert out["ok"] is True
    assert seen["creds"].client_secret == "shh-secret"
    assert "1 account allowed" in out["message"]
    assert "1 new under DETECTED" in out["message"]

    tid = ctrader_terminal_id(7)
    assert cfg.terminals[tid].ctrader_account_id == 7
    assert cfg.terminals[tid].role is Role.UNASSIGNED
    assert ("sync", None) in engine.calls  # persisted and the engine woken


def test_an_assigned_ctrader_slave_reconnects_for_the_new_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = ctrader_terminal_id(7)
    cfg = AppConfig(
        terminals={
            tid: TerminalConfig(
                terminal_id=tid, label="IC cTrader", ctrader_account_id=7, role=Role.SLAVE
            )
        }
    )
    api, engine, _ = _api(cfg)
    monkeypatch.setattr(
        ctrader_auth,
        "log_in",
        lambda *a, **k: [CTraderAccount(account_id=7, is_live=False, login=111, broker="IC")],
    )
    out = api.ctrader_login("app-id", "shh", "http://127.0.0.1:9290/callback")
    assert out["ok"] is True
    assert "new under DETECTED" not in out["message"]  # already known, only relabelled
    assert ("reconnect", tid) in engine.calls


def test_failures_come_back_as_the_message_and_clear_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, _ = _api()

    def refused(*a: Any, **k: Any) -> list:
        raise LoginError("cTrader refused the login: access denied")

    monkeypatch.setattr(ctrader_auth, "log_in", refused)
    out = api.ctrader_login("app-id", "shh", "http://localhost:9290/callback")
    assert out["ok"] is False
    assert "refused" in out["message"]
    assert api.ctrader_login_state()["waiting"] is False

    # A login that comes back with nothing ticked is said, not celebrated.
    monkeypatch.setattr(ctrader_auth, "log_in", lambda *a, **k: [])
    out = api.ctrader_login("app-id", "shh", "http://localhost:9290/callback")
    assert out["ok"] is False
    assert "no account was ticked" in out["message"]
