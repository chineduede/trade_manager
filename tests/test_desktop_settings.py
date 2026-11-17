"""The desktop Settings page's bridge: the copier's own settings read, checked and saved.

What a value means, its bounds and the two gates on the mode are mt5copier/general_edit.py's
and are pinned by tests/test_general_edit.py; these tests pin the bridge's translation -- the
state the form fills from, the as-you-type check, the LIVE handshake, and that a mode switch
refused by the engine refuses the whole save.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mt5copier.config import AppConfig, AppMode, Role, TerminalConfig
from mt5copier.desktop.api import DesktopApi


class _Engine:
    """Only what the settings bridge reaches for, recorded rather than done."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self.calls: list[tuple[Any, ...]] = []
        self.mode_refusal: str | None = None

    def set_mode(self, mode: AppMode) -> str | None:
        self.calls.append(("set_mode", mode))
        if self.mode_refusal is not None:
            return self.mode_refusal
        self._cfg.mode = mode
        return None

    def sync(self) -> None:
        self.calls.append(("sync",))


def _cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        copying_enabled=False,
        terminals={
            "t-ftmo": TerminalConfig(
                terminal_id="t-ftmo",
                label="FTMO",
                exe_path=exe,
                role=Role.SLAVE,
                last_login=51234,
            ),
        },
    )


def _api(cfg: AppConfig) -> tuple[DesktopApi, _Engine]:
    engine = _Engine(cfg)
    api = DesktopApi(cfg=cfg, engine=engine)  # type: ignore[arg-type]
    return api, engine


def _form(cfg: AppConfig, **edits: str | bool) -> dict[str, str | bool]:
    """What the page would send: every field as the form holds it, with edits over it."""
    from mt5copier.general_edit import initial_general

    raw = initial_general(cfg)
    raw.update(edits)
    return raw


def test_state_carries_the_form_values_and_the_mode_facts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, _ = _api(cfg)
    out = api.settings_state()
    assert out["profileMode"] == "copy"
    assert out["copying"] is False
    assert out["dryRun"] is True
    v = out["values"]
    assert v["dry_run"] is True
    assert v["poll_interval_ms"] == str(cfg.poll_interval_ms)
    assert v["close_terminals"] == cfg.close_terminals.value
    assert "," in v["reconnect_backoff_s"] or v["reconnect_backoff_s"]


def test_check_words_errors_and_changes_without_saving(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, _ = _api(cfg)
    before = cfg.poll_interval_ms

    bad = api.settings_check(_form(cfg, poll_interval_ms="nope", history_days="0"))
    assert "whole number" in bad["errors"]["poll_interval_ms"]
    assert bad["errors"]["history_days"]
    assert bad["changes"] == ""

    good = api.settings_check(_form(cfg, poll_interval_ms="1500"))
    assert good["errors"] == {}
    assert "poll_interval_ms" in good["changes"]
    assert cfg.poll_interval_ms == before  # a check never writes

    live = api.settings_check(_form(cfg, dry_run=False))
    assert live["goesLive"] is True


def test_going_live_is_a_handshake_and_the_wrong_word_refuses(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, _ = _api(cfg)

    asked = api.settings_save(_form(cfg, dry_run=False), "copy")
    assert asked["ok"] is False
    assert asked["needLive"] is True
    assert asked["slaves"] == ["FTMO"]
    assert cfg.dry_run is True

    wrong = api.settings_save(_form(cfg, dry_run=False), "copy", typed="live please")
    assert wrong["ok"] is False
    assert "not LIVE" in wrong["errors"]["dry_run"]
    assert cfg.dry_run is True

    done = api.settings_save(_form(cfg, dry_run=False), "copy", typed="LIVE")
    assert done["ok"] is True
    assert cfg.dry_run is False
    texts = [m["text"] for m in done["messages"]]
    assert any(t.startswith("Saved: dry_run") for t in texts)
    assert any("LIVE: real orders" in t for t in texts)


def test_the_mode_gate_is_worded_for_this_window(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.copying_enabled = True
    api, _ = _api(cfg)
    out = api.settings_check(_form(cfg, dry_run=False))
    assert "halt it (the title bar button)" in out["errors"]["dry_run"]
    assert "with d" not in out["errors"]["dry_run"]


def test_a_refused_mode_switch_refuses_the_whole_save(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, engine = _api(cfg)
    engine.mode_refusal = "1 open copy still open; close them first"
    before = cfg.poll_interval_ms

    out = api.settings_save(_form(cfg, poll_interval_ms="1500"), "trading")
    assert out["ok"] is False
    assert out["errors"]["profile_mode"] == engine.mode_refusal
    # Refused whole: the interval edit that was valid is not half-applied.
    assert cfg.poll_interval_ms == before
    assert cfg.mode is AppMode.COPY


def test_saving_commits_switches_and_says_what_applies_later(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, engine = _api(cfg)

    none = api.settings_save(_form(cfg), "copy")
    assert none["ok"] is True
    assert none["messages"][0]["text"] == "The copier's settings: no changes."
    assert ("sync",) not in engine.calls

    out = api.settings_save(_form(cfg, poll_interval_ms="1500", history_days="14"), "trading")
    assert out["ok"] is True
    assert cfg.poll_interval_ms == 1500
    assert cfg.history_days == 14
    assert cfg.mode is AppMode.TRADING
    assert ("set_mode", AppMode.TRADING) in engine.calls
    assert ("sync",) in engine.calls
    texts = [m["text"] for m in out["messages"]]
    assert any(t.startswith("Saved: ") for t in texts)
    assert any("trades directly" in t for t in texts)
    assert any("next time its connection starts" in t for t in texts)


# --- the symbol catalog card -------------------------------------------------------------------


def _catalogued(cfg: AppConfig, tmp_path: Path) -> DesktopApi:
    from mt5copier.catalog import CatalogStore

    api, _ = _api(cfg)
    api._catalog_store = CatalogStore(tmp_path / "catalog.json")
    return api


def test_catalog_state_offers_the_seeds_before_any_save(tmp_path: Path) -> None:
    api = _catalogued(_cfg(tmp_path), tmp_path)
    out = api.catalog_state()
    assert "USD" in out["currencies"] and "EUR" in out["currencies"]
    assert "US30" in out["classes"]["indices"]
    assert out["classes"]["stocks"] == ""


def test_catalog_save_is_whole_and_read_back(tmp_path: Path) -> None:
    api = _catalogued(_cfg(tmp_path), tmp_path)
    out = api.catalog_save("usd, eur sgd", {"indices": "US30 Dow30", "stocks": "AAPL"})
    assert out["ok"] is True and "3 currencies" in out["message"]
    state = api.catalog_state()
    assert state["currencies"] == "EUR SGD USD"
    assert state["classes"]["indices"] == "US30, DOW30"
    assert state["classes"]["stocks"] == "AAPL"
    assert state["classes"]["metals"] == "", "the save is the whole truth: seeds not sent are gone"


def test_catalog_save_refuses_junk_per_field(tmp_path: Path) -> None:
    api = _catalogued(_cfg(tmp_path), tmp_path)
    out = api.catalog_save("USD EURO", {"indices": "US30", "metals": "X" * 40})
    assert out["ok"] is False
    assert "EURO" in out["errors"]["currencies"]
    assert "metals" in out["errors"]
    assert not (tmp_path / "catalog.json").exists(), "refused whole: nothing was written"
