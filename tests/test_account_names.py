"""Naming an account from the desktop bridge.

``settings_edit.rename`` itself -- stripping, clearing, keying by account -- is pinned in
``test_settings_edit.py``, and ``display_name``'s fallback chain in ``test_config.py``. What
these pin is the wiring on top: what the desktop's ``rename_account`` says and saves.
"""

from __future__ import annotations

from pathlib import Path

from mt5copier.config import AppConfig, Role, TerminalConfig, account_key
from mt5copier.desktop.api import DesktopApi

TID = "A" * 32


class _Engine:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def sync(self) -> None:
        self.calls.append("sync")


def _terminal(role: Role = Role.UNASSIGNED, known: bool = True) -> TerminalConfig:
    return TerminalConfig(
        terminal_id=TID,
        label="Spare",
        exe_path=Path("t.exe"),
        role=role,
        last_login=51234 if known else None,
        last_server="FTMO-Demo" if known else None,
    )


# --- the desktop bridge ----------------------------------------------------------------------


def _api(role: Role = Role.SLAVE, known: bool = True) -> tuple[DesktopApi, _Engine]:
    engine = _Engine()
    cfg = AppConfig(terminals={TID: _terminal(role, known)})
    return DesktopApi(cfg=cfg, engine=engine), engine  # type: ignore[arg-type]


def test_rename_account_names_the_account_and_saves() -> None:
    api, engine = _api()
    out = api.rename_account(TID, "  FTMO 100k  ")
    assert out["ok"] is True
    assert out["message"] == "51234@FTMO-Demo is now called FTMO 100k."
    key = account_key(51234, "FTMO-Demo")
    assert api._cfg.account_names[key] == "FTMO 100k"
    assert api._cfg.display_name(TID) == "FTMO 100k"
    assert "sync" in engine.calls, "the engine redraws from the new name"


def test_rename_account_to_blank_clears_the_name() -> None:
    api, _ = _api()
    api.rename_account(TID, "FTMO 100k")
    out = api.rename_account(TID, "   ")
    assert out["ok"] is True
    assert "cleared" in out["message"]
    assert api._cfg.account_names == {}
    assert api._cfg.display_name(TID) == "Spare"


def test_rename_account_refuses_with_no_account_and_a_gone_terminal() -> None:
    api, engine = _api(known=False)
    out = api.rename_account(TID, "FTMO 100k")
    assert out["ok"] is False
    assert "no account is known" in out["message"]
    assert engine.calls == [], "nothing saved, nothing synced"
    assert api.rename_account("t-nothing", "x")["ok"] is False


def test_the_name_follows_the_account_not_the_terminal() -> None:
    """The point of the key: the other terminal on the same account shows the name too."""
    other = "B" * 32
    api, _ = _api()
    api._cfg.terminals[other] = TerminalConfig(
        terminal_id=other,
        label="Other",
        role=Role.UNASSIGNED,
        last_login=51234,
        last_server="FTMO-Demo",
    )
    api.rename_account(TID, "FTMO 100k")
    assert api._cfg.display_name(other) == "FTMO 100k"
