"""The desktop Setups page's bridge: saved setups worded as rows, and the asking a load goes
through.

What a setup holds and what loading one restores live in mt5copier/setups.py and are pinned by
tests/test_setups.py; these tests pin the bridge's translation -- the rows, the NOW line, the
ask's sentence and stake, and that a commit hands the engine exactly what was agreed to.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mt5copier import setups as setups_mod
from mt5copier.config import AppConfig, AppMode, Role, TerminalConfig
from mt5copier.desktop.api import DesktopApi
from mt5copier.setups import Setup, SetupsDoc

T_M = "t-master"
T_A = "t-ftmo"
T_B = "t-e8"


@dataclass
class _Copy:
    master_ticket: int
    slave_ticket: int | None
    symbol: str
    volume: float
    resting: bool = False


class _Engine:
    """Only what the setups bridge reaches for, recorded rather than done."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self.calls: list[tuple[Any, ...]] = []
        self.refusal: str | None = None
        self.stake: dict[str, list[_Copy]] = {}

    def logins(self) -> dict[str, int | None]:
        return {tid: t.last_login for tid, t in self._cfg.terminals.items()}

    def setup_stake(self, setup: Setup) -> dict[str, list[_Copy]]:
        return self.stake

    def load_setup(
        self,
        setup: Setup,
        *,
        orphan: Mapping[str, Collection[int]] | None = None,
        leave_out: Collection[str] = (),
        go_live: bool = False,
    ) -> str | None:
        self.calls.append(("load", setup, orphan, tuple(leave_out), go_live))
        if self.refusal is not None:
            return self.refusal
        setups_mod.apply_roles(setup, self._cfg, leave_out)
        setups_mod.restore_settings(setup, self._cfg)
        return None

    def sync(self) -> None:
        self.calls.append(("sync",))


def _cfg(tmp_path: Path, mode: AppMode = AppMode.COPY) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")

    def t(tid: str, label: str, role: Role, login: int) -> TerminalConfig:
        return TerminalConfig(
            terminal_id=tid,
            label=label,
            exe_path=exe,
            role=role,
            last_login=login,
            last_server=f"{label}-Demo",
        )

    return AppConfig(
        mode=mode,
        copying_enabled=True,
        terminals={
            T_M: t(T_M, "Master", Role.MASTER, 111),
            T_A: t(T_A, "FTMO", Role.SLAVE, 51234),
            T_B: t(T_B, "E8", Role.SLAVE, 20481),
        },
    )


def _api(cfg: AppConfig) -> tuple[DesktopApi, _Engine]:
    engine = _Engine(cfg)
    api = DesktopApi(cfg=cfg, engine=engine, guards=None, setups=SetupsDoc())  # type: ignore[arg-type]
    return api, engine


def test_saving_words_the_row_and_marks_it_current(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, _ = _api(cfg)

    assert api.setup_save("  ")["ok"] is False
    done = api.setup_save("scalping week")
    assert done["ok"] is True

    out = api.setups_state()
    assert out["direct"] is False
    assert out["current"] == "scalping week"
    assert out["loading"] is False
    (row,) = out["setups"]
    assert row["name"] == "scalping week"
    assert row["master"] == "Master · 111"
    assert row["slaves"] == "FTMO · 51234, E8 · 20481"
    assert row["copying"] is True
    assert row["dryRun"] == cfg.dry_run
    assert row["saved"].startswith("today ")
    assert row["current"] is True
    assert row["problems"] == []
    # The NOW line is the same sentence a load's confirmation uses.
    assert out["now"].startswith("master Master · 111, 2 slaves")
    assert "settings as saved" in out["now"]

    again = api.setup_save("scalping week")
    assert again["ok"] is False
    assert "already saved" in again["message"]


def test_nothing_to_save_without_a_master_or_without_slaves_direct(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.set_role(T_M, Role.UNASSIGNED)
    api, _ = _api(cfg)
    assert api.setups_state()["now"] is None
    refused = api.setup_save("anything")
    assert refused["ok"] is False
    assert "no master" in refused["message"]

    direct = _cfg(tmp_path, mode=AppMode.TRADING)
    direct.set_role(T_A, Role.UNASSIGNED)
    direct.set_role(T_B, Role.UNASSIGNED)
    api, _ = _api(direct)
    assert api.setups_state()["now"] is None
    refused = api.setup_save("anything")
    assert refused["ok"] is False
    assert "no slaves" in refused["message"]


def test_a_direct_profile_saves_a_masterless_setup(tmp_path: Path) -> None:
    direct = _cfg(tmp_path, mode=AppMode.TRADING)
    api, _ = _api(direct)
    assert api.setup_save("eval week")["ok"] is True
    out = api.setups_state()
    assert out["direct"] is True
    assert out["current"] == "eval week"
    assert out["now"].startswith("trades directly, 2 slaves")
    (row,) = out["setups"]
    assert row["master"] == "trades directly"
    assert row["slaves"] == "FTMO · 51234, E8 · 20481"
    assert row["problems"] == []
    saved = api._setups.setups["eval week"]
    assert saved.master is None and saved.direct
    assert set(saved.accounts) == {T_A, T_B}, "the virtual master has no account to keep"


def test_a_setup_loads_only_into_its_own_mode(tmp_path: Path) -> None:
    # A direct setup offered to a copy profile, and the other way around: each is refused
    # with the way out named, on the row and at the load.
    direct = _cfg(tmp_path, mode=AppMode.TRADING)
    api, _ = _api(direct)
    api.setup_save("eval week")
    doc = api._setups

    copying = _cfg(tmp_path)
    api, _ = _api(copying)
    api._setups.setups.update(doc.setups)
    api.setup_save("copy week")
    (eval_row,) = [r for r in api.setups_state()["setups"] if r["name"] == "eval week"]
    assert any("trades directly" in p for p in eval_row["problems"])
    refused = api.setup_load_ask("eval week")
    assert refused["ok"] is False and "direct trading" in refused["message"]

    direct2 = _cfg(tmp_path, mode=AppMode.TRADING)
    api, _ = _api(direct2)
    api._setups.setups.update(doc.setups)
    api._setups.setups["copy week"] = setups_mod.capture(copying) or doc.setups["eval week"]
    (copy_row,) = [r for r in api.setups_state()["setups"] if r["name"] == "copy week"]
    assert any("copies a master" in p for p in copy_row["problems"])
    refused = api.setup_load_ask("copy week")
    assert refused["ok"] is False and "copying" in refused["message"]


def test_loading_a_direct_setup_restores_slaves_and_copying(tmp_path: Path) -> None:
    direct = _cfg(tmp_path, mode=AppMode.TRADING)
    api, engine = _api(direct)
    api.setup_save("eval week")
    # Drift: one slave unassigned, copying off.
    direct.set_role(T_B, Role.UNASSIGNED)
    direct.copying_enabled = False
    ask = api.setup_load_ask("eval week")
    assert ask["ok"] is True
    assert ask["changes"].startswith("trades directly, 2 slaves")
    done = api.setup_load("eval week", None, False)
    assert done["ok"] is True
    assert direct.terminals[T_B].role is Role.SLAVE
    assert direct.copying_enabled is True
    assert ("sync",) in engine.calls


def test_rename_and_delete_keep_the_file_honest(tmp_path: Path) -> None:
    api, _ = _api(_cfg(tmp_path))
    api.setup_save("old")
    assert api.setup_rename("old", "old")["ok"] is True
    assert api.setup_rename("old", "")["ok"] is False
    api.setup_save("taken")
    assert api.setup_rename("old", "taken")["ok"] is False
    assert api.setup_rename("old", "new")["ok"] is True
    assert sorted(api._setups.setups) == ["new", "taken"]
    assert api.setup_delete("new")["ok"] is True
    assert api.setup_delete("new")["ok"] is False
    assert sorted(api._setups.setups) == ["taken"]


def test_the_ask_says_the_change_and_the_copies_at_stake(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, engine = _api(cfg)
    api.setup_save("both")
    # One in place already asks for nothing.
    already = api.setup_load_ask("both")
    assert already["ok"] is False
    assert already.get("already") is True

    # A setup without E8 strands E8's copies; the engine says which.
    smaller = setups_mod.capture(cfg, engine.logins())
    assert smaller is not None
    api._setups.setups["ftmo only"] = smaller.model_copy(
        update={"slaves": (T_A,), "copying": False}
    )
    engine.stake = {T_B: [_Copy(101, 9001, "BTCUSD", 0.4), _Copy(102, None, "XAUUSD", 0.3, True)]}

    out = api.setup_load_ask("ftmo only")
    assert out["ok"] is True
    assert out["changes"].startswith("master Master · 111, 1 slave (FTMO · 51234), copying off")
    (staked,) = out["stake"]
    assert staked["name"] == "E8"
    assert [c["masterTicket"] for c in staked["copies"]] == [101, 102]
    assert staked["copies"][1]["resting"] is True
    assert out["goesLive"] is False
    assert out["live"] is False
    assert out["switching"] == []

    gone = api.setup_load_ask("no such")
    assert gone["ok"] is False


def test_loading_hands_the_engine_what_was_agreed(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, engine = _api(cfg)
    api.setup_save("both")
    smaller = setups_mod.capture(cfg, engine.logins())
    assert smaller is not None
    api._setups.setups["ftmo only"] = smaller.model_copy(
        update={"slaves": (T_A,), "copying": False}
    )

    out = api.setup_load(name="ftmo only", agreed={T_B: [101]}, go_live=False)
    assert out["ok"] is True
    load = next(c for c in engine.calls if c[0] == "load")
    assert load[2] == {T_B: [101]}
    assert load[3] == ()
    assert load[4] is False
    assert cfg.terminals[T_B].role is Role.UNASSIGNED
    assert cfg.copying_enabled is False
    texts = [m["text"] for m in out["messages"]]
    assert any("copying is off" in t for t in texts)
    assert any("orphaned" in t for t in texts)

    engine.refusal = "commands are still in flight; try again in a moment"
    refused = api.setup_load(name="both", agreed=None, go_live=False)
    assert refused["ok"] is False
    assert "was not loaded" in refused["messages"][0]["text"]


def test_a_live_setup_says_so_and_carries_the_typed_live(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert cfg.dry_run is True
    api, engine = _api(cfg)
    api.setup_save("hot")
    api._setups.setups["hot"] = api._setups.setups["hot"].model_copy(
        update={"dry_run": False, "copying": True, "slaves": (T_A,)}
    )

    out = api.setup_load_ask("hot")
    assert out["ok"] is True
    assert out["goesLive"] is True
    assert out["live"] is True
    assert out["slaves"] == ["FTMO"]

    done = api.setup_load(name="hot", agreed=None, go_live=True)
    assert done["ok"] is True
    load = next(c for c in engine.calls if c[0] == "load")
    assert load[4] is True
    assert cfg.dry_run is False
    assert any("COPYING LIVE" in m["text"] for m in done["messages"])
