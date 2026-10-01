"""FastAPI routes against a faked Engine -- same fakery pattern ``test_tui.py``'s ``StubEngine``
uses, so no broker or real ``Engine`` is needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.engine import TerminalView
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
            received_at=0.0,
        ),
        TerminalView(cfg=cfg.terminals[TID_SLAVE], supervised=False),
    ]
    return eng


@pytest.fixture
def client(engine: FakeEngine) -> TestClient:
    with TestClient(create_app(engine, token="test-token")) as c:
        yield c


def test_index_requires_a_token(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 401


def test_index_rejects_the_wrong_token(client: TestClient) -> None:
    resp = client.get("/?token=wrong")
    assert resp.status_code == 401


def test_index_shows_known_terminals_and_their_status(client: TestClient) -> None:
    resp = client.get("/?token=test-token")
    assert resp.status_code == 200
    assert "IC Markets Global" in resp.text
    assert "BlackBull Markets" in resp.text
    assert "connected" in resp.text
    assert "not connected" in resp.text


def test_index_shows_a_blocked_reason_when_detection_is_paused(engine: FakeEngine) -> None:
    engine.blocked_reason = "no master assigned"
    with TestClient(create_app(engine, token="test-token")) as c:
        resp = c.get("/?token=test-token")
    assert "no master assigned" in resp.text


def test_engine_is_started_and_stopped_via_the_app_lifespan(engine: FakeEngine) -> None:
    app = create_app(engine, token="test-token")
    with TestClient(app) as c:
        assert engine.started is True
        assert engine.stopped is False
        c.get("/?token=test-token")
    assert engine.stopped is True
