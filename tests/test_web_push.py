"""Websocket push: a bad token is rejected before ``accept()``, fragments arrive on each tick,
and a connection only ever receives log lines appended after it connected.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.engine import TerminalView
from mt5copier.paths import log_dir
from mt5copier.web import routes as routes_mod
from mt5copier.web.app import create_app

TID = "A" * 32


class FakeEngine:
    def __init__(self, cfg: AppConfig) -> None:
        self.config = cfg
        self.views_list: list[TerminalView] = []
        self.blocked_reason: str | None = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def views(self) -> list[TerminalView]:
        return list(self.views_list)

    def health(self) -> dict[str, int]:
        return {"total": len(self.views_list), "supervised": 0, "live": 0, "stale": 0}

    def holds(self) -> dict[str, str]:
        return {}


@pytest.fixture(autouse=True)
def _fast_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real 0.5s cadence (matched to the TUI's own timer) would make every test here slow
    for no reason -- these tests only check content, not real-time pacing."""
    monkeypatch.setattr(routes_mod, "PUSH_INTERVAL_S", 0.01)


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig(
        terminals={TID: TerminalConfig(terminal_id=TID, label="Alpha Broker", role=Role.SLAVE)}
    )


@pytest.fixture
def engine(cfg: AppConfig) -> FakeEngine:
    eng = FakeEngine(cfg)
    eng.views_list = [TerminalView(cfg=cfg.terminals[TID], supervised=False)]
    return eng


@pytest.fixture
def client(engine: FakeEngine) -> TestClient:
    with TestClient(create_app(engine, token="test-token")) as c:
        yield c


def test_a_wrong_token_is_rejected_before_accept(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/state?token=wrong&log_offset=0"):
            pass


def test_a_missing_token_is_rejected_too(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/state?log_offset=0"):
            pass


def test_correct_token_receives_health_then_one_row_fragment(client: TestClient) -> None:
    with client.websocket_connect("/ws/state?token=test-token&log_offset=0") as ws:
        health_frag = ws.receive_text()
        row_frag = ws.receive_text()
    assert 'id="health-bar"' in health_frag
    assert f'id="row-{TID}"' in row_frag
    assert "Alpha Broker" in row_frag


def test_a_connection_only_sees_log_lines_appended_after_it_connected(client: TestClient) -> None:
    log_path = log_dir() / "copier.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line before the dashboard connected\n")
    offset_at_connect = log_path.stat().st_size

    with client.websocket_connect(
        f"/ws/state?token=test-token&log_offset={offset_at_connect}"
    ) as ws:
        ws.receive_text()  # first tick: health
        ws.receive_text()  # first tick: the one row -- no log fragment, nothing new yet

        log_path.write_text(log_path.read_text() + "line written after connecting\n")

        ws.receive_text()  # second tick: health
        ws.receive_text()  # second tick: the one row
        append_frag = ws.receive_text()  # second tick: the new log line

    assert "line written after connecting" in append_frag
    assert "line before the dashboard connected" not in append_frag
    assert "hx-swap-oob" in append_frag
