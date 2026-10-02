"""Routes against a faked Engine -- same fakery pattern ``test_tui.py``'s ``StubEngine`` uses,
so no broker or real ``Engine`` is needed. Covers all three pages (home, terminals, log) and
their partial routes (what htmx polls), including that a partial returns only its fragment --
not a full page, which would otherwise nest the whole page inside itself on every poll.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.engine import TerminalView
from mt5copier.paths import log_dir
from mt5copier.protocol import Snapshot, WorkerState
from mt5copier.web.app import create_app

TID_MASTER = "A" * 32
TID_SLAVE = "B" * 32


class FakeEngine:
    """Engine stand-in exposing exactly the read surface Stage A's routes use."""

    def __init__(self, cfg: AppConfig) -> None:
        self.config = cfg
        self.views_list: list[TerminalView] = []
        self.holds_dict: dict[str, str] = {}
        self.blocked_reason: str | None = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def views(self) -> list[TerminalView]:
        return list(self.views_list)

    def health(self) -> dict[str, int]:
        return {
            "total": len(self.views_list),
            "supervised": sum(1 for v in self.views_list if v.supervised),
            "live": sum(
                1 for v in self.views_list if v.snapshot is not None and v.snapshot.is_live
            ),
            "stale": 0,
        }

    def holds(self) -> dict[str, str]:
        return dict(self.holds_dict)


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig(
        terminals={
            TID_MASTER: TerminalConfig(
                terminal_id=TID_MASTER, label="IC Markets Global", role=Role.MASTER
            ),
            TID_SLAVE: TerminalConfig(
                terminal_id=TID_SLAVE, label="BlackBull Markets", role=Role.SLAVE
            ),
        }
    )


@pytest.fixture
def engine(cfg: AppConfig) -> FakeEngine:
    eng = FakeEngine(cfg)
    eng.views_list = [
        TerminalView(
            cfg=cfg.terminals[TID_MASTER],
            snapshot=Snapshot(
                terminal_id=TID_MASTER, state=WorkerState.CONNECTED, balance=1000.0, equity=1000.0
            ),
            supervised=True,
            process_alive=True,
            # time.monotonic(), not 0.0: staleness is judged against time.monotonic(), which on
            # this machine starts in the millions (time since boot, not since the epoch), so a
            # literal 0.0 always reads as wildly stale regardless of intent.
            received_at=time.monotonic(),
        ),
        TerminalView(cfg=cfg.terminals[TID_SLAVE], supervised=False),
    ]
    return eng


@pytest.fixture
def client(engine: FakeEngine) -> TestClient:
    with TestClient(create_app(engine, token="test-token")) as c:
        yield c


ALL_ROUTES = ["/", "/terminals", "/log", "/partials/health", "/partials/terminals", "/partials/log"]


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_every_route_requires_a_token(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401
    assert client.get(path, params={"token": "wrong"}).status_code == 401


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_every_route_admits_the_correct_token(client: TestClient, path: str) -> None:
    assert client.get(path, params={"token": "test-token"}).status_code == 200


def _nav_tag(html: str, label: str) -> str:
    """The opening ``<a ...>`` tag for the nav link with this label, so "is it marked current"
    can be checked on the *right* link -- not just "the word current appears somewhere," which
    is exactly the kind of substring check that let ``received_at=0.0`` ship broken earlier
    (``"connected" in text`` is trivially true whenever "not connected" is on the page too)."""
    marker = f">{label}</a>"
    end = html.index(marker)
    start = html.rindex("<a ", 0, end)
    return html[start : end + len(marker)]


def test_home_shows_health_and_nav(client: TestClient) -> None:
    resp = client.get("/", params={"token": "test-token"})
    assert "SUPERVISED" in resp.text
    assert "1/2" in resp.text  # one of the two terminals is supervised
    assert "current" in _nav_tag(resp.text, "HOME")
    assert "current" not in _nav_tag(resp.text, "TERMINALS")
    assert "current" not in _nav_tag(resp.text, "LOG")


def test_home_shows_a_blocked_reason_when_detection_is_paused(engine: FakeEngine) -> None:
    engine.blocked_reason = "no master assigned"
    with TestClient(create_app(engine, token="test-token")) as c:
        resp = c.get("/", params={"token": "test-token"})
    assert "no master assigned" in resp.text


def test_terminals_page_shows_known_terminals_and_their_status(client: TestClient) -> None:
    resp = client.get("/terminals", params={"token": "test-token"})
    assert "IC Markets Global" in resp.text
    assert "BlackBull Markets" in resp.text
    assert "[OK]" in resp.text  # the good-tier badge for the connected master
    assert "[OFF]" in resp.text  # the muted-tier badge for the unsupervised slave
    assert ">connected<" in resp.text
    assert ">not connected<" in resp.text


def test_log_page_shows_the_tail(client: TestClient) -> None:
    log_path = log_dir() / "copier.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"2026-10-02 01:00:00 INFO    [MainProcess] x: hello from the log\n")
    resp = client.get("/log", params={"token": "test-token"})
    assert "hello from the log" in resp.text


def test_partial_health_returns_only_the_fragment_not_a_full_page(client: TestClient) -> None:
    resp = client.get("/partials/health", params={"token": "test-token"})
    assert "SUPERVISED" in resp.text
    assert "<html" not in resp.text
    assert "<nav" not in resp.text


def test_partial_terminals_returns_only_the_fragment_not_a_full_page(client: TestClient) -> None:
    resp = client.get("/partials/terminals", params={"token": "test-token"})
    assert "IC Markets Global" in resp.text
    assert "<html" not in resp.text
    assert "<nav" not in resp.text


def test_partial_log_returns_only_the_fragment_not_a_full_page(client: TestClient) -> None:
    log_path = log_dir() / "copier.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"2026-10-02 01:00:00 INFO    [MainProcess] x: partial log line\n")
    resp = client.get("/partials/log", params={"token": "test-token"})
    assert "partial log line" in resp.text
    assert "<html" not in resp.text


def test_engine_is_started_and_stopped_via_the_app_lifespan(engine: FakeEngine) -> None:
    app = create_app(engine, token="test-token")
    with TestClient(app) as c:
        assert engine.started is True
        assert engine.stopped is False
        c.get("/", params={"token": "test-token"})
    assert engine.stopped is True
