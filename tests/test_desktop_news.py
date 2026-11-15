"""The desktop News page's bridge: the week's releases with the guard's answer to each.

The wording rules live in the bridge (the guard column is composed there), but what counts
and when the rule would act come from the news rule's own parameters -- these tests pin the
translation, not the rule, which tests/test_news_guard.py already holds.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.desktop.api import DesktopApi, _news_said
from mt5copier.engine import TerminalView
from mt5copier.guards import GuardRule, GuardsDoc
from mt5copier.guards.model import AccountGuards
from mt5copier.news import NewsCalendar
from mt5copier.protocol import Position, PositionType

TID = "t-slave"


def _position(symbol: str) -> Position:
    return Position(
        ticket=1,
        symbol=symbol,
        type=PositionType.BUY,
        volume=1.0,
        price_open=1.0,
        sl=0.9,
        tp=0.0,
    )


class _Engine:
    def __init__(self, calendar: NewsCalendar, cfg: AppConfig) -> None:
        self._calendar = calendar
        self._cfg = cfg
        self.direct: list[Position] = []
        self.slave_positions: list[Position] = []

    def news_calendar(self) -> NewsCalendar:
        return self._calendar

    def views(self) -> list[TerminalView]:
        return [
            TerminalView(cfg=t.model_copy(deep=True), snapshot=None)
            for t in self._cfg.terminals.values()
        ]

    def direct_positions(self) -> list[Position]:
        return self.direct

    def direct_orders(self) -> list[Any]:
        return []


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat()


def _cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        terminals={
            TID: TerminalConfig(
                terminal_id=TID,
                label="FTMO",
                exe_path=exe,
                role=Role.SLAVE,
                last_login=51234,
                last_server="FTMO-Demo",
            )
        }
    )


def _calendar(tmp_path: Path, feed: object) -> NewsCalendar:
    cal = NewsCalendar(path=tmp_path / "news.json", fetch=lambda: feed)
    assert cal.refresh() is True
    return cal


def _doc(cfg: AppConfig, params: dict[str, Any]) -> GuardsDoc:
    doc = GuardsDoc()
    doc.set_account(
        "51234@FTMO-Demo",
        AccountGuards(rules=(GuardRule(type="news", params=params),)),
    )
    return doc


def _api(tmp_path: Path, feed: object, params: dict[str, Any] | None = None) -> DesktopApi:
    cfg = _cfg(tmp_path)
    engine = _Engine(_calendar(tmp_path, feed), cfg)
    doc = _doc(cfg, params) if params is not None else GuardsDoc()
    return DesktopApi(cfg=cfg, engine=engine, guards=doc)  # type: ignore[arg-type]


def test_the_week_reads_with_the_guards_answer(tmp_path: Path) -> None:
    now = time.time()
    feed = [
        # Yesterday's release stays off the page; today starts the week shown.
        {"date": _iso(now - 86400 * 2), "country": "usd", "impact": "High", "title": "Old"},
        {
            "date": _iso(now + 3600),
            "country": "usd",
            "impact": "High",
            "title": "CPI m/m",
            "forecast": "0.3%",
            "previous": "0.2%",
        },
        {"date": _iso(now + 7200), "country": "usd", "impact": "Medium", "title": "Oil"},
        {"date": _iso(now + 10800), "country": "gbp", "impact": "Low", "title": "Speech"},
    ]
    out = _api(tmp_path, feed, {"before_news": "breakeven", "act_before": 15.0}).news_state()

    assert out["known"] is True
    assert out["ruled"] is True
    assert out["scanned"] != ""
    assert [e["title"] for e in out["events"]] == ["CPI m/m", "Oil", "Speech"]

    cpi, oil, speech = out["events"]
    assert cpi["guarded"] is True
    assert cpi["forecast"] == "0.3%"
    assert cpi["previous"] == "0.2%"
    acts = time.strftime("%H:%M", time.localtime(now + 3600 - 15 * 60))
    assert cpi["guard"] == f"acts {acts} · breakeven open copies, close losers, cancel resting USD"
    # The rule is high-impact only, and the cell says so instead of going quiet.
    assert oil["guarded"] is False
    assert oil["guard"] == "rule counts high impact only"
    assert speech["guard"] == ""

    nxt = out["next"]
    assert nxt is not None
    assert nxt["title"] == "CPI m/m"
    assert nxt["clock"] == time.strftime("%H:%M", time.localtime(now + 3600))
    assert nxt["guard"].endswith("· guards FTMO")
    assert "USD" in out["currencies"] and "GBP" in out["currencies"]


def test_without_an_enforced_rule_nothing_is_promised(tmp_path: Path) -> None:
    now = time.time()
    feed = [{"date": _iso(now + 3600), "country": "usd", "impact": "High", "title": "CPI"}]
    out = _api(tmp_path, feed).news_state()
    assert out["ruled"] is False
    assert out["next"] is None
    assert out["events"][0]["guarded"] is False
    assert out["events"][0]["guard"] == ""


def test_a_rule_that_only_declines_shows_its_window(tmp_path: Path) -> None:
    now = time.time()
    feed = [{"date": _iso(now + 3600), "country": "eur", "impact": "High", "title": "ECB"}]
    out = _api(
        tmp_path, feed, {"before_news": "nothing", "before": 10.0, "after": 5.0}
    ).news_state()
    start = time.strftime("%H:%M", time.localtime(now + 3600 - 600))
    end = time.strftime("%H:%M", time.localtime(now + 3600 + 300))
    assert out["events"][0]["guard"] == f"no new EUR trades {start}–{end}"


def test_an_unreadable_calendar_says_why(tmp_path: Path) -> None:
    def failing() -> object:
        raise OSError("connection reset")

    cfg = _cfg(tmp_path)
    cal = NewsCalendar(path=tmp_path / "news.json", fetch=failing)
    api = DesktopApi(cfg=cfg, engine=_Engine(cal, cfg), guards=GuardsDoc())  # type: ignore[arg-type]
    out = api.news_state()
    assert out["known"] is False
    assert out["said"] != ""
    assert out["events"] == []

    answer = api.news_rescan()
    assert answer["ok"] is False
    assert "the last copy stands" in answer["message"]


def test_rescan_reports_the_week(tmp_path: Path) -> None:
    now = time.time()
    feed = [{"date": _iso(now + 3600), "country": "usd", "impact": "High", "title": "CPI"}]
    assert _api(tmp_path, feed).news_rescan() == {
        "ok": True,
        "message": "calendar scanned: 1 releases this week",
    }


def test_my_currencies_come_from_the_open_book(tmp_path: Path) -> None:
    now = time.time()
    feed = [{"date": _iso(now + 3600), "country": "usd", "impact": "High", "title": "CPI"}]
    cfg = _cfg(tmp_path)
    engine = _Engine(_calendar(tmp_path, feed), cfg)
    api = DesktopApi(cfg=cfg, engine=engine, guards=GuardsDoc())  # type: ignore[arg-type]

    assert api.news_state()["myCurrencies"] == []
    engine.direct = [_position("m.EURUSDp"), _position("XAUUSD")]
    assert api.news_state()["myCurrencies"] == ["EUR", "USD", "XAU"]
    # A name that cannot be read is moved by any news, so the filter cannot narrow.
    engine.direct.append(_position("WIG20"))
    assert api.news_state()["myCurrencies"] is None


def test_the_wording_covers_every_action(tmp_path: Path) -> None:
    now = time.time()
    feed = [{"date": _iso(now + 3600), "country": "usd", "impact": "High", "title": "CPI"}]
    out = _api(
        tmp_path, feed, {"before_news": "close", "act_before": 10.0, "affects": "all"}
    ).news_state()
    acts = time.strftime("%H:%M", time.localtime(now + 3600 - 600))
    assert out["events"][0]["guard"] == f"acts {acts} · close open copies, cancel all resting"


def test_said_scopes_resting_to_the_currency() -> None:
    from mt5copier.news import NewsEvent

    e = NewsEvent(when=1000000.0, currency="ALL", impact="High", title="OPEC")
    said = _news_said({"before_news": "close", "act_before": 5.0, "affects": "currency"}, e)
    # 'ALL' belongs to no one currency, so even a currency-scoped rule cancels everything.
    assert said.endswith("close open copies, cancel all resting")
