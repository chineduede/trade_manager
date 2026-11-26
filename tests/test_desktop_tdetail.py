"""The desktop terminal detail page's bridge: one terminal's settings read, checked and saved.

What a value means and that an edit keeps open copies are mt5copier/settings_edit.py's and the
engine's, pinned by their own tests; these pin the bridge's translation -- the state each role's
form fills from, the as-you-type check with its worked example, the save path, and copying
another slave's settings without its broker's own names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mt5copier import shared_db
from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.desktop.api import DesktopApi

T_M = "t-master"
T_A = "t-ftmo"
T_B = "t-e8"


class _Engine:
    """Only what the detail bridge reaches for, recorded rather than done."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self.calls: list[tuple[Any, ...]] = []
        self.refusal: str | None = None

    def views(self) -> list[Any]:
        return []

    def holds(self) -> dict[str, str]:
        return {}

    def day_book(self) -> list[Any]:
        return []

    def open_copies(self, tid: str) -> list[Any]:
        return []

    def recent_results(self) -> list[Any]:
        return []

    def copies_on_real_account(self, tid: str) -> int:
        return 0

    def apply_settings(self, new: TerminalConfig) -> str | None:
        self.calls.append(("apply", new))
        if self.refusal is not None:
            return self.refusal
        self._cfg.terminals[new.terminal_id] = new
        return None

    def sync(self) -> None:
        self.calls.append(("sync",))


def _cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")

    def t(tid: str, label: str, role: Role) -> TerminalConfig:
        return TerminalConfig(terminal_id=tid, label=label, exe_path=exe, role=role)

    return AppConfig(
        terminals={
            T_M: t(T_M, "Axi", Role.MASTER),
            T_A: t(T_A, "FTMO", Role.SLAVE),
            T_B: t(T_B, "E8", Role.SLAVE),
        }
    )


def _api(cfg: AppConfig) -> tuple[DesktopApi, _Engine]:
    engine = _Engine(cfg)
    api = DesktopApi(cfg=cfg, engine=engine)  # type: ignore[arg-type]
    return api, engine


def test_a_slaves_state_carries_the_form_and_its_footing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.terminals[T_A].symbol_map = {"US30": "US30.cash", "GER40": "DE40"}
    api, _ = _api(cfg)

    out = api.terminal_detail_state(T_A)
    assert out["ok"] is True
    assert out["role"] == "slave"
    assert out["name"] == "FTMO"
    assert out["accountKind"] == "unread"  # no view, so no snapshot
    v = out["values"]
    assert v["risk_percent"] == format(cfg.terminals[T_A].risk_percent, ".15g")
    assert v["symbol_map"] == "GER40=DE40, US30=US30.cash"
    assert out["masterSays"] is True  # the master's default management is MASTER
    assert out["stake"]["text"].startswith("Nothing is open on this slave")
    assert out["copyFrom"] == [{"tid": T_B, "name": "E8"}]
    # No master trade to work on: the example says so rather than inventing numbers.
    assert "no open trade" in out["example"]["lead"]

    gone = api.terminal_detail_state("t-gone")
    assert gone["ok"] is False


def test_the_masters_state_is_its_own_smaller_form(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, _ = _api(cfg)
    out = api.terminal_detail_state(T_M)
    assert out["role"] == "master"
    assert sorted(out["values"]) == [
        "breakeven_book_percent",
        "breakeven_mode",
        "breakeven_trigger_r",
        "management",
        "symbol_prefix",
        "symbol_suffix",
    ]
    # Nothing held: the decoration example falls back to EURUSD, undecorated.
    assert out["symbolExample"] == [{"symbol": "EURUSD", "plain": "EURUSD"}]


def test_check_words_errors_and_the_example_without_saving(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, _ = _api(cfg)
    before = cfg.terminals[T_A].risk_percent

    bad = api.terminal_settings_check(T_A, {"risk_percent": "a lot"})
    assert bad["errors"]["risk_percent"]
    assert bad["changes"] == ""
    assert bad["example"]["lead"].startswith("Correct the settings")

    good = api.terminal_settings_check(T_A, {"risk_percent": "0.25"})
    assert good["errors"] == {}
    assert "risk_percent" in good["changes"]
    assert cfg.terminals[T_A].risk_percent == before  # a check never writes

    deco = api.terminal_settings_check(
        T_M, {"symbol_prefix": "", "symbol_suffix": ".r", "management": "slaves"}
    )
    assert deco["errors"] == {}
    assert deco["symbolExample"] == [{"symbol": "EURUSD.r", "plain": "EURUSD"}]


def test_saving_goes_through_the_engine_and_warns_like_the_tui(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, engine = _api(cfg)

    none = api.terminal_settings_save(T_A, {"risk_percent": str(cfg.terminals[T_A].risk_percent)})
    assert none["ok"] is True
    assert "no changes" in none["messages"][0]["text"]
    assert not engine.calls

    out = api.terminal_settings_save(
        T_A, {"risk_percent": "0.25", "no_stop_policy": "lot_multiple"}
    )
    assert out["ok"] is True
    assert cfg.terminals[T_A].risk_percent == 0.25
    texts = [m["text"] for m in out["messages"]]
    assert any(t.startswith("FTMO: ") and "risk_percent" in t for t in texts)
    # The TUI's advisory rides along: no-stop trades now copy unprotected.
    assert any("UNPROTECTED" in t for t in texts)
    assert ("sync",) in engine.calls

    engine.refusal = "the slave is mid-swap; try again"
    refused = api.terminal_settings_save(T_A, {"risk_percent": "1.5"})
    assert refused["ok"] is False
    assert refused["message"] == engine.refusal
    assert cfg.terminals[T_A].risk_percent == 0.25


def test_copying_from_another_slave_keeps_this_brokers_names(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    src = cfg.terminals[T_B]
    cfg.terminals[T_B] = src.model_copy(
        update={"risk_percent": 2.0, "symbol_suffix": ".x", "symbol_map": {"US30": "US30.z"}}
    )
    api, _ = _api(cfg)

    out = api.terminal_settings_from(T_B)
    assert out["ok"] is True
    assert out["name"] == "E8"
    assert out["values"]["risk_percent"] == "2"
    for brokers_own in ("symbol_prefix", "symbol_suffix", "symbol_map"):
        assert brokers_own not in out["values"]

    assert api.terminal_settings_from(T_M)["ok"] is False  # not a slave


def test_a_save_banks_the_settings_for_the_account(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.terminals[T_A].last_login = 51234
    cfg.terminals[T_A].last_server = "FTMO-Demo"
    api, _ = _api(cfg)

    out = api.terminal_settings_save(T_A, {"risk_percent": "0.75"})
    assert out["ok"] is True
    banked = shared_db.get().fetch("account_policies", "51234@FTMO-Demo")
    assert banked is not None and banked["risk_percent"] == 0.75


def test_a_bank_from_another_window_is_folded_in_by_the_poll(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.terminals[T_A].last_login = 51234
    cfg.terminals[T_A].last_server = "FTMO-Demo"
    api, engine = _api(cfg)

    api._refresh_policies()  # nothing banked yet: records the stamp, changes nothing
    assert engine.calls == []

    db = shared_db.get()
    with db.change(shared_db.POLICIES):
        db.put("account_policies", "51234@FTMO-Demo", {"risk_percent": 3.25, "invert": True})
    api._refresh_policies()
    tcfg = cfg.terminals[T_A]
    assert (tcfg.risk_percent, tcfg.invert) == (3.25, True)
    assert any(c[0] == "apply" for c in engine.calls)

    # The same bank again: the stamp short-circuits, nothing is re-applied.
    engine.calls.clear()
    api._refresh_policies()
    assert engine.calls == []
