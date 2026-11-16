"""The direct-trading window's smaller controls: the symbol search, partial close and
close-everything bridges, and the plain-failure toasts beside the guard ones."""

from __future__ import annotations

import time
from typing import Any

from mt5copier.config import AppConfig, TerminalConfig
from mt5copier.day_book import Outcome
from mt5copier.desktop.api import DesktopApi
from mt5copier.history import Step


class _Engine:
    def __init__(self) -> None:
        self.symbols: tuple[str, ...] = ()
        self.problems: list[Step] = []
        self.calls: list[tuple[str, Any]] = []

    def feed_symbols(self) -> tuple[str, ...]:
        return self.symbols

    def recent_problems(self, since: float) -> list[Step]:
        return [s for s in self.problems if s.ts >= since]

    def partial_close_direct(self, ticket: int, percent: float) -> str | None:
        self.calls.append(("partial", (ticket, percent)))
        return None

    def flatten_direct(self) -> int:
        self.calls.append(("flatten", None))
        return 3

    def sync(self) -> None:
        pass


def _api() -> tuple[DesktopApi, _Engine]:
    engine = _Engine()
    cfg = AppConfig(terminals={"t1": TerminalConfig(terminal_id="t1", label="IC demo")})
    return DesktopApi(cfg=cfg, engine=engine), engine  # type: ignore[arg-type]


def test_symbol_search_offers_clean_base_names_prefix_first() -> None:
    api, engine = _api()
    engine.symbols = ("EURUSD.r", "GBPUSD.r", "EURJPY.r", "XAUUSD.r", "CADJPY.r")
    out = api.symbol_search("JPY")
    # Prefix matches would come first; none start with JPY, so these are the contains
    # matches in catalog order, each stripped of the broker's .r decoration.
    assert out["symbols"] == ["EURJPY", "CADJPY"]
    assert out["known"] == 5
    assert api.symbol_search("EUR")["symbols"] == ["EURUSD", "EURJPY"]
    assert api.symbol_search("")["symbols"][0] == "EURUSD"


def test_symbol_search_is_empty_while_the_feed_has_pushed_nothing() -> None:
    api, _ = _api()
    assert api.symbol_search("EUR") == {"symbols": [], "known": 0}


def test_partial_close_checks_the_number_before_the_engine_hears_of_it() -> None:
    api, engine = _api()
    out = api.partial_close(7, "half of it")
    assert out["ok"] is False
    assert engine.calls == []
    out = api.partial_close(7, "25")
    assert out["ok"] is True
    assert "25% of #7" in out["message"]
    assert engine.calls == [("partial", (7, 25.0))]


def test_flatten_says_how_much_it_closed() -> None:
    api, engine = _api()
    out = api.flatten()
    assert out["ok"] is True
    assert "3 position(s) and order(s)" in out["message"]
    assert engine.calls == [("flatten", None)]


def test_problem_events_carry_the_failure_and_its_reason() -> None:
    api, engine = _api()
    now = time.time()
    engine.problems = [
        Step(
            ts=now - 5.0,
            terminal_id="t1",
            master_ticket=42,
            symbol="XAUUSD",
            label="buy",
            kind="open",
            outcome=str(Outcome.REFUSED),
            problem=True,
            master="0.01 lots at 4,000",
            slave="computed size is under the symbol's minimum volume",
            action="open refused",
        )
    ]
    [e] = api._problem_events(now)
    assert e["slave"] == "IC demo"
    assert e["did"] == "open refused"
    assert e["trade"] == "#42 XAUUSD buy"
    assert "minimum volume" in e["detail"]
    assert e["key"]  # stable identity, so a toast fires once
