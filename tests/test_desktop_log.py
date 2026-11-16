"""The desktop log dock's bridge: copier.log and the day book merged, narrowed, worded.

Parsing, attribution and the merge live in mt5copier/log_entries.py and are pinned by
tests/test_log_entries.py; these pin the bridge's translation -- the Who wording, the tones
the dock styles by, the filters, and the honest counts under them.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mt5copier import paths
from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.day_book import MasterEvent, Outcome, SlaveOutcome
from mt5copier.desktop import api as api_mod
from mt5copier.desktop.api import DesktopApi
from mt5copier.protocol import ActionKind

T_M = "t-master"
T_A = "t-ftmo"


class _Engine:
    def __init__(self, events: list[MasterEvent], since: float) -> None:
        self._events = events
        self._since = since

    def day_book(self, limit: int | None = None) -> list[MasterEvent]:
        return self._events

    @property
    def day_book_since(self) -> float:
        return self._since


def _cfg(tmp_path: Path) -> AppConfig:
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
        terminals={
            T_M: t(T_M, "IC", Role.MASTER, 111),
            T_A: t(T_A, "FTMO", Role.SLAVE, 51234),
        }
    )


def _write_log(now: float) -> None:
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    clock = lambda s: f"{day} {s}"  # noqa: E731
    log_path = paths.log_dir() / "copier.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"{clock('09:00:01')} INFO    [MainProcess] mt5copier.engine: spawned worker for FTMO\n"
        f"{clock('09:00:05')} WARNING [MainProcess] mt5copier.worker: FTMO heartbeat is stale\n"
        f"{clock('09:00:09')} INFO    [MainProcess] mt5copier.config: config saved\n",
        encoding="utf-8",
    )


def _event(now: float) -> MasterEvent:
    return MasterEvent(
        id=1,
        ts=now - 60,
        kind=ActionKind.OPEN,
        preexisting=False,
        master_ticket=11,
        symbol="BTCUSD",
        label="buy",
        volume=0.78,
        volume_before=None,
        entry=84854.0,
        sl=84532.7,
        tp=85165.81,
        outcomes=(SlaveOutcome(T_A, Outcome.DONE, lots=3.11, latency_ms=360, ts=now - 59),),
    )


def _api(tmp_path: Path, now: float) -> DesktopApi:
    _write_log(now)
    engine = _Engine([_event(now)], since=now - 3600)
    return DesktopApi(cfg=_cfg(tmp_path), engine=engine)  # type: ignore[arg-type]


def test_the_file_and_the_day_book_merge_with_the_tui_wording(tmp_path: Path) -> None:
    now = time.time()
    out = _api(tmp_path, now).log_state("", False, "")

    assert out["matched"] == 5
    assert out["problems"] == 1
    assert [t["name"] for t in out["terminals"]] == ["IC", "FTMO"]

    whos = [r["who"] for r in out["rows"]]
    assert whos[:3] == ["Slave 1 (FTMO)", "Slave 1 (FTMO)", "copier"]
    assert whos[3:] == ["Master (IC)", "Slave 1 (FTMO)"], "the day book's lines come after"

    stale = out["rows"][1]
    assert stale["tone"] == "warn"
    assert stale["text"] == "FTMO heartbeat is stale"
    assert stale["when"] == "09:00:05"

    master = out["rows"][3]
    assert master["tone"] == "master"
    assert master["text"].startswith("Opened BTCUSD buy 0.78")
    assert out["rows"][4]["tone"] == "done"


def test_the_filters_narrow_in_the_bridge_and_the_counts_stay_honest(tmp_path: Path) -> None:
    now = time.time()
    api = _api(tmp_path, now)

    one = api.log_state(T_A, False, "")
    assert one["matched"] == 3, "the slave's file lines and its day-book outcome"
    assert one["problems"] == 1

    # Problems-only shows the one line but counts against the same scope.
    problems = api.log_state("", True, "")
    assert [r["text"] for r in problems["rows"]] == ["FTMO heartbeat is stale"]
    assert (problems["matched"], problems["problems"]) == (1, 1)

    # The search reaches what a line shows: the who finds a terminal's lines too.
    named = api.log_state("", False, "slave 1")
    assert named["matched"] == 3
    assert api.log_state("", False, "nothing-like-this")["matched"] == 0


def test_the_tail_is_capped_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    api = _api(tmp_path, now)
    monkeypatch.setattr(api_mod, "_LOG_SHOWN", 2)
    out = api.log_state("", False, "")
    assert out["matched"] == 5
    assert len(out["rows"]) == 2
    assert out["rows"][-1]["tone"] == "done", "the newest lines are the ones kept"


def test_an_empty_log_reads_as_nothing_not_an_error(tmp_path: Path) -> None:
    engine = _Engine([], since=time.time())
    api = DesktopApi(cfg=_cfg(tmp_path), engine=engine)  # type: ignore[arg-type]
    out = api.log_state("", False, "")
    assert out["rows"] == []
    assert (out["matched"], out["problems"]) == (0, 0)
