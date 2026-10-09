"""cTrader: the Open API client's framing, the conversion to the copier's own types, the login,
the config's cTrader accounts, and a worker run end to end against a fake Open API server."""

from __future__ import annotations

import asyncio
import queue
import struct
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from mt5copier import config as config_mod
from mt5copier import role_actions
from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.ctrader import auth
from mt5copier.ctrader import convert as cv
from mt5copier.ctrader import worker as ct_worker
from mt5copier.ctrader.client import CTraderClient, OpenApiError, decode, encode
from mt5copier.ctrader.proto import OpenApiMessages_pb2 as oa
from mt5copier.ctrader.proto import OpenApiModelMessages_pb2 as om
from mt5copier.protocol import (
    ACCOUNT_TRADE_MODE_DEMO,
    COPIER_MAGIC,
    ActionKind,
    OrderCommand,
    OrderResult,
    OrderType,
    PositionType,
    Snapshot,
    WorkerState,
)

ACCOUNT = 4_200_001
EURUSD = cv.SymbolInfo(symbol_id=1, name="EURUSD", digits=5, lot_size=10_000_000)


# --- framing -------------------------------------------------------------------------------------


def test_a_message_round_trips_through_a_frame() -> None:
    frame = encode(oa.ProtoOATraderReq(ctidTraderAccountId=ACCOUNT), "abc")
    (length,) = struct.unpack(">I", frame[:4])
    assert length == len(frame) - 4
    msg, client_msg_id = decode(frame[4:])
    assert isinstance(msg, oa.ProtoOATraderReq)
    assert msg.ctidTraderAccountId == ACCOUNT
    assert client_msg_id == "abc"


# --- conversion ----------------------------------------------------------------------------------


def _trade(side: int, volume: int, label: str = "", ts: int = 1_700_000_000_123) -> Any:
    return om.ProtoOATradeData(
        symbolId=1, volume=volume, tradeSide=side, openTimestamp=ts, label=label, comment="c"
    )


def test_a_position_is_in_lots_seconds_and_marked_ours_by_its_label() -> None:
    raw = om.ProtoOAPosition(
        positionId=77,
        tradeData=_trade(om.SELL, 2_500_000, label=cv.COPIER_LABEL),
        positionStatus=om.POSITION_STATUS_OPEN,
        swap=0,
        price=1.08512,
        stopLoss=1.0900,
    )
    pos = cv.position(raw, EURUSD)
    assert pos.ticket == 77
    assert pos.type is PositionType.SELL
    assert pos.volume == pytest.approx(0.25)
    assert pos.price_open == pytest.approx(1.08512)
    assert pos.sl == pytest.approx(1.09)
    assert pos.tp == 0.0
    assert pos.time == 1_700_000_000
    assert pos.magic == COPIER_MAGIC


def test_a_position_without_the_label_is_someone_elses() -> None:
    raw = om.ProtoOAPosition(
        positionId=1,
        tradeData=_trade(om.BUY, 10_000_000, label="manual"),
        positionStatus=om.POSITION_STATUS_OPEN,
        swap=0,
        price=1.0,
    )
    assert cv.position(raw, EURUSD).magic == 0


def test_resting_orders_take_mt5s_types_and_others_are_left_out() -> None:
    limit = om.ProtoOAOrder(
        orderId=5,
        tradeData=_trade(om.BUY, 10_000_000),
        orderType=om.LIMIT,
        orderStatus=om.ORDER_STATUS_ACCEPTED,
        limitPrice=1.07,
    )
    got = cv.pending_order(limit, EURUSD)
    assert got is not None
    assert got.type is OrderType.BUY_LIMIT
    assert got.price_open == pytest.approx(1.07)
    assert got.volume == pytest.approx(1.0)

    market = om.ProtoOAOrder(
        orderId=6,
        tradeData=_trade(om.BUY, 10_000_000),
        orderType=om.MARKET,
        orderStatus=om.ORDER_STATUS_ACCEPTED,
    )
    assert cv.pending_order(market, EURUSD) is None


def test_a_stop_limit_orders_slippage_becomes_mt5s_limit_price() -> None:
    def order(side: int) -> Any:
        return om.ProtoOAOrder(
            orderId=8,
            tradeData=_trade(side, 10_000_000),
            orderType=om.STOP_LIMIT,
            orderStatus=om.ORDER_STATUS_ACCEPTED,
            stopPrice=1.10000,
            slippageInPoints=20,
        )

    buy = cv.pending_order(order(om.BUY), EURUSD)
    sell = cv.pending_order(order(om.SELL), EURUSD)
    assert buy is not None and sell is not None
    assert buy.type is OrderType.BUY_STOP_LIMIT
    assert buy.price_stoplimit == pytest.approx(1.10020)
    assert sell.price_stoplimit == pytest.approx(1.09980)


def test_money_is_scaled_by_the_accounts_digits() -> None:
    assert cv.money(123_456, 2) == pytest.approx(1234.56)


# --- the login -----------------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_with(redirect: str, url: str) -> str:
    """Run the listener, make the browser's request to it, and return the code."""
    result: dict[str, Any] = {}

    def listen() -> None:
        try:
            result["code"] = auth.wait_for_code(redirect, timeout=10)
        except auth.LoginError as exc:
            result["error"] = exc

    thread = threading.Thread(target=listen)
    thread.start()
    deadline = time.monotonic() + 5
    while True:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    thread.join(10)
    if "error" in result:
        raise result["error"]
    return str(result["code"])


def test_the_listener_takes_the_code_from_the_redirect() -> None:
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    assert _wait_with(redirect, f"{redirect}?code=XYZ") == "XYZ"


def test_the_listener_reports_a_refusal() -> None:
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    with pytest.raises(auth.LoginError, match="access_denied"):
        _wait_with(redirect, f"{redirect}?error=access_denied")


def test_the_authorise_url_names_the_app_and_its_redirect() -> None:
    creds = auth.CTraderCredentials(client_id="123_abc", redirect_uri="http://127.0.0.1:9290/cb")
    url = auth.authorize_url(creds)
    assert url.startswith(auth.AUTHORIZE_URL)
    assert "client_id=123_abc" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A9290%2Fcb" in url
    assert "scope=trading" in url


def test_credentials_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ctrader.json"
    creds = auth.CTraderCredentials(
        client_id="id",
        client_secret="secret",
        access_token="a",
        accounts=[auth.CTraderAccount(account_id=ACCOUNT, is_live=False, login=9, broker="X")],
    )
    auth.save(creds, path)
    again = auth.load(path)
    assert again == creds
    assert again.account(ACCOUNT) is not None


def test_a_corrupt_credentials_file_is_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "ctrader.json"
    path.write_text("{not json", encoding="utf-8")
    assert auth.load(path) == auth.CTraderCredentials()
    assert not path.exists()


def test_a_token_is_refreshed_only_when_close_to_expiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ctrader.json"
    calls: list[dict[str, str]] = []

    def fake_call(params: dict[str, str]) -> dict[str, object]:
        calls.append(params)
        return {"accessToken": "new", "refreshToken": "r2", "expiresIn": 2_628_000}

    monkeypatch.setattr(auth, "_token_call", fake_call)
    fresh = auth.CTraderCredentials(
        client_id="i",
        client_secret="s",
        access_token="old",
        refresh_token="r1",
        expires_at=time.time() + 20 * 86400,
    )
    auth.save(fresh, path)
    assert auth.refresh_if_due(path) is False
    assert calls == []

    auth.save(fresh.model_copy(update={"expires_at": time.time() + 3600}), path)
    assert auth.refresh_if_due(path) is True
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "r1"
    saved = auth.load(path)
    assert (saved.access_token, saved.refresh_token) == ("new", "r2")
    assert saved.expires_at > time.time() + 29 * 86400


def test_a_refused_token_is_a_login_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Answer:
        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"errorCode": "ACCESS_DENIED", "description": "bad code"}'

    monkeypatch.setattr(auth.urllib.request, "urlopen", lambda *a, **k: Answer())
    with pytest.raises(auth.LoginError, match="bad code"):
        auth.exchange_code(auth.CTraderCredentials(client_id="i", client_secret="s"), "c")


# --- config --------------------------------------------------------------------------------------


def _cfg_with_master() -> AppConfig:
    cfg = AppConfig()
    cfg.terminals["M" * 32] = TerminalConfig(
        terminal_id="M" * 32, label="MT5", exe_path=Path("C:/x/terminal64.exe")
    )
    cfg.set_role("M" * 32, Role.MASTER)
    return cfg


def test_allowed_accounts_become_terminals_once() -> None:
    cfg = AppConfig()
    added = config_mod.merge_ctrader_accounts(cfg, [(ACCOUNT, False, 9001, "Pepperstone")])
    tid = config_mod.ctrader_terminal_id(ACCOUNT)
    assert added == [tid]
    tcfg = cfg.terminals[tid]
    assert tcfg.is_ctrader and tcfg.usable and tcfg.exe_path is None
    assert tcfg.last_login == 9001
    assert tcfg.last_server == "Pepperstone-cTrader-Demo"
    assert config_mod.merge_ctrader_accounts(cfg, [(ACCOUNT, False, 9001, "Pepperstone")]) == []


def test_a_ctrader_account_can_be_a_slave_but_never_the_master() -> None:
    cfg = _cfg_with_master()
    config_mod.merge_ctrader_accounts(cfg, [(ACCOUNT, False, 9001, "B")])
    tid = config_mod.ctrader_terminal_id(ACCOUNT)
    assert role_actions.set_slave(cfg, tid) is None
    assert cfg.terminals[tid].role is Role.SLAVE

    role_actions.unassign(cfg, "M" * 32)
    problem = role_actions.set_master(cfg, tid)
    assert problem is not None and "only be a slave" in problem
    assert cfg.master is None


def test_a_config_with_a_ctrader_master_is_refused() -> None:
    cfg = AppConfig()
    config_mod.merge_ctrader_accounts(cfg, [(ACCOUNT, False, 1, "B")])
    tid = config_mod.ctrader_terminal_id(ACCOUNT)
    raw = cfg.model_dump(mode="json")
    raw["terminals"][tid]["role"] = "master"
    with pytest.raises(ValueError, match="only be a slave"):
        AppConfig.model_validate(raw)


def test_ctrader_settings_survive_a_save_and_load(tmp_path: Path) -> None:
    cfg = _cfg_with_master()
    config_mod.merge_ctrader_accounts(cfg, [(ACCOUNT, True, 1, "B")])
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    again = config_mod.load(path).terminals[config_mod.ctrader_terminal_id(ACCOUNT)]
    assert again.ctrader_account_id == ACCOUNT
    assert again.ctrader_live is True


# --- the worker, against a fake Open API server --------------------------------------------------


class FakeOpenApi:
    """Answers the requests a worker makes, from canned state."""

    def __init__(self, *, refuse_account: str | None = None) -> None:
        self.refuse_account = refuse_account
        self.seen: list[str] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()

    def _answer(self, msg: Any) -> Any:
        name = type(msg).__name__
        self.seen.append(name)
        if isinstance(msg, oa.ProtoOAApplicationAuthReq):
            return oa.ProtoOAApplicationAuthRes()
        if isinstance(msg, oa.ProtoOAAccountAuthReq):
            if self.refuse_account:
                return oa.ProtoOAErrorRes(errorCode=self.refuse_account, description="no")
            return oa.ProtoOAAccountAuthRes(ctidTraderAccountId=ACCOUNT)
        if isinstance(msg, oa.ProtoOATraderReq):
            return oa.ProtoOATraderRes(
                ctidTraderAccountId=ACCOUNT,
                trader=om.ProtoOATrader(
                    ctidTraderAccountId=ACCOUNT,
                    balance=1_000_000,
                    depositAssetId=15,
                    leverageInCents=10_000,
                    traderLogin=9001,
                    brokerName="Broker",
                    moneyDigits=2,
                ),
            )
        if isinstance(msg, oa.ProtoOAAssetListReq):
            return oa.ProtoOAAssetListRes(
                ctidTraderAccountId=ACCOUNT, asset=[om.ProtoOAAsset(assetId=15, name="USD")]
            )
        if isinstance(msg, oa.ProtoOASymbolsListReq):
            return oa.ProtoOASymbolsListRes(
                ctidTraderAccountId=ACCOUNT,
                symbol=[om.ProtoOALightSymbol(symbolId=1, symbolName="EURUSD")],
            )
        if isinstance(msg, oa.ProtoOASymbolByIdReq):
            return oa.ProtoOASymbolByIdRes(
                ctidTraderAccountId=ACCOUNT,
                symbol=[om.ProtoOASymbol(symbolId=1, digits=5, pipPosition=4, lotSize=10_000_000)],
            )
        if isinstance(msg, oa.ProtoOAReconcileReq):
            return oa.ProtoOAReconcileRes(
                ctidTraderAccountId=ACCOUNT,
                position=[
                    om.ProtoOAPosition(
                        positionId=77,
                        tradeData=_trade(om.BUY, 5_000_000, label=cv.COPIER_LABEL),
                        positionStatus=om.POSITION_STATUS_OPEN,
                        swap=-150,
                        price=1.1,
                        usedMargin=2_500,
                    )
                ],
            )
        if isinstance(msg, oa.ProtoOAGetPositionUnrealizedPnLReq):
            return oa.ProtoOAGetPositionUnrealizedPnLRes(
                ctidTraderAccountId=ACCOUNT,
                moneyDigits=2,
                positionUnrealizedPnL=[
                    om.ProtoOAPositionUnrealizedPnL(
                        positionId=77, grossUnrealizedPnL=1_300, netUnrealizedPnL=1_200
                    )
                ],
            )
        return oa.ProtoOAErrorRes(errorCode="UNEXPECTED", description=name)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readexactly(4)
                (length,) = struct.unpack(">I", head)
                msg, client_msg_id = decode(await reader.readexactly(length))
                if msg is None or client_msg_id is None:
                    continue
                writer.write(encode(self._answer(msg), client_msg_id))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()


def _creds() -> auth.CTraderCredentials:
    return auth.CTraderCredentials(client_id="i", client_secret="s", access_token="tok")


async def _run_worker(api: FakeOpenApi, commands: list[object], ticks: float = 1.2) -> list[object]:
    out_q: queue.Queue[object] = queue.Queue()
    cmd_q: queue.Queue[object] = queue.Queue()
    for c in commands:
        cmd_q.put(c)
    account = ct_worker._Account(
        ACCOUNT,
        False,
        "Broker-cTrader-Demo",
        lambda host: CTraderClient("127.0.0.1", api.port, tls=False),
        _creds,
    )
    task = asyncio.create_task(
        ct_worker._run("CTRADER:1", account, out_q, cmd_q, 0.05, [0.05])  # type: ignore[arg-type]
    )
    await asyncio.sleep(ticks)
    cmd_q.put(None)
    await asyncio.wait_for(task, 5)
    found: list[object] = []
    while not out_q.empty():
        found.append(out_q.get_nowait())
    return found


async def test_the_worker_reports_the_account_like_an_mt5_terminal() -> None:
    api = FakeOpenApi()
    await api.start()
    try:
        got = await _run_worker(api, [])
    finally:
        await api.stop()
    live = [s for s in got if isinstance(s, Snapshot) and s.state is WorkerState.CONNECTED]
    assert live, [getattr(s, "error", None) for s in got]
    snap = live[-1]
    assert snap.login == 9001
    assert snap.server == "Broker-cTrader-Demo"
    assert snap.currency == "USD"
    assert snap.balance == pytest.approx(10_000.0)
    assert snap.profit == pytest.approx(12.0)
    assert snap.equity == pytest.approx(10_012.0)
    assert snap.margin == pytest.approx(25.0)
    assert snap.leverage == 100
    assert snap.trade_mode == ACCOUNT_TRADE_MODE_DEMO
    assert snap.server_offset_s == 0
    assert snap.deals is None
    assert snap.open_positions is not None and len(snap.open_positions) == 1
    pos = snap.open_positions[0]
    assert (pos.ticket, pos.symbol, pos.volume, pos.magic) == (77, "EURUSD", 0.5, COPIER_MAGIC)
    assert snap.live_positions is not None
    assert snap.live_positions[0].swap == pytest.approx(-1.5)
    assert snap.pending_orders == ()
    assert isinstance(got[-1], Snapshot) and got[-1].state is WorkerState.STOPPED


async def test_the_worker_refuses_every_order_for_now() -> None:
    api = FakeOpenApi()
    await api.start()
    cmd = OrderCommand(
        command_id=3,
        op=ActionKind.OPEN,
        master_ticket=11,
        master_symbol="EURUSD",
        direction=PositionType.BUY,
    )
    try:
        got = await _run_worker(api, [cmd], ticks=0.3)
    finally:
        await api.stop()
    results = [r for r in got if isinstance(r, OrderResult)]
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].retryable is False
    assert results[0].error == ct_worker.NOT_TRADING_YET
    # Nothing reached the server that could place an order.
    assert not any("Order" in name and "Req" in name for name in api.seen)


async def test_an_expired_login_says_to_log_in_again() -> None:
    api = FakeOpenApi(refuse_account="CH_ACCESS_TOKEN_INVALID")
    await api.start()
    try:
        got = await _run_worker(api, [], ticks=0.3)
    finally:
        await api.stop()
    failed = [s for s in got if isinstance(s, Snapshot) and s.state is WorkerState.FAILED]
    assert failed
    assert failed[0].error is not None and "press t" in failed[0].error


async def test_a_worker_without_a_login_does_not_connect() -> None:
    account = ct_worker._Account(
        ACCOUNT,
        False,
        "x",
        lambda host: CTraderClient("127.0.0.1", 1, tls=False),
        auth.CTraderCredentials,
    )
    ok, err = await account.connect()
    assert ok is False
    assert err is not None and "press t" in err


async def test_an_error_answer_raises() -> None:
    api = FakeOpenApi()
    await api.start()
    client = CTraderClient("127.0.0.1", api.port, tls=False)
    await client.connect()
    try:
        with pytest.raises(OpenApiError, match="UNEXPECTED"):
            await client.request(oa.ProtoOADealListReq(ctidTraderAccountId=1, fromTimestamp=0))
    finally:
        await client.close()
        await api.stop()


def test_the_menu_offers_a_ctrader_account_only_the_slave_role() -> None:
    from mt5copier.tui.menu import terminal_actions

    cfg = AppConfig()
    config_mod.merge_ctrader_accounts(cfg, [(ACCOUNT, False, 1, "B")])
    tcfg = cfg.terminals[config_mod.ctrader_terminal_id(ACCOUNT)]
    without = terminal_actions(tcfg, has_master=False, orphans_waiting=False)
    assert "m" not in [i.key for i in without]
    assert any("only be a slave" in i.label for i in without)
    keys = [i.key for i in terminal_actions(tcfg, has_master=True, orphans_waiting=False)]
    assert "s" in keys
    assert "m" not in keys and "i" not in keys


# --- the login, through the UI -------------------------------------------------------------------


async def test_t_logs_in_and_the_allowed_accounts_appear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import Input, Static
    from tui_support import make_app, make_cfg

    from mt5copier.tui.ctrader import CTraderDialog

    creds_path = tmp_path / "ctrader.json"
    monkeypatch.setattr(auth, "ctrader_path", lambda: creds_path)
    used: list[auth.CTraderCredentials] = []

    def fake_log_in(
        creds: auth.CTraderCredentials, path: object = None, cancel: object = None
    ) -> list[auth.CTraderAccount]:
        used.append(creds)
        return [auth.CTraderAccount(account_id=ACCOUNT, is_live=False, login=9001, broker="B")]

    monkeypatch.setattr(auth, "log_in", fake_log_in)
    cfg = make_cfg(tmp_path)
    app = make_app(cfg, tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.press("t")
        dialog = app.screen
        assert isinstance(dialog, CTraderDialog)
        assert dialog.query_one("#redirect", Input).value == auth.DEFAULT_REDIRECT_URI

        await pilot.press("enter")
        assert "both needed" in str(dialog.query_one("#error", Static).render())

        dialog.query_one("#client-id", Input).value = "123_app"
        dialog.query_one("#secret", Input).value = "s3cret"
        dialog.query_one("#redirect", Input).focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.02)
            if config_mod.ctrader_terminal_id(ACCOUNT) in cfg.terminals:
                break
        assert used and used[0].client_id == "123_app"
        assert auth.load(creds_path).client_secret == "s3cret"
        tcfg = cfg.terminals[config_mod.ctrader_terminal_id(ACCOUNT)]
        assert tcfg.role is Role.UNASSIGNED
        assert config_mod.ctrader_terminal_id(ACCOUNT) in app._order


def test_a_localhost_redirect_is_answered_on_ipv4_and_ipv6() -> None:
    import socket

    port = _free_port()
    redirect = f"http://localhost:{port}/callback"
    assert _wait_with(redirect, f"http://127.0.0.1:{port}/callback?code=V4") == "V4"
    if socket.has_ipv6:
        try:
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind(("::1", 0))
        except OSError:
            return
        assert _wait_with(redirect, f"http://[::1]:{port}/callback?code=V6") == "V6"


def test_a_waiting_login_can_be_given_up() -> None:
    cancel = threading.Event()
    result: dict[str, BaseException] = {}

    def listen() -> None:
        try:
            auth.wait_for_code(f"http://127.0.0.1:{_free_port()}/cb", timeout=30, cancel=cancel)
        except BaseException as exc:
            result["exc"] = exc

    thread = threading.Thread(target=listen)
    thread.start()
    time.sleep(0.2)
    began = time.monotonic()
    cancel.set()
    thread.join(5)
    assert not thread.is_alive()
    assert time.monotonic() - began < 2
    assert isinstance(result["exc"], auth.LoginCancelled)


async def test_t_while_a_login_waits_gives_it_up_and_asks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tui_support import make_app, make_cfg

    from mt5copier.tui.ctrader import CTraderDialog

    monkeypatch.setattr(auth, "ctrader_path", lambda: tmp_path / "ctrader.json")
    auth.save(auth.CTraderCredentials(client_id="i", client_secret="s"), tmp_path / "ctrader.json")
    given_up: list[bool] = []

    def waits(
        creds: auth.CTraderCredentials, path: object = None, cancel: Any = None
    ) -> list[auth.CTraderAccount]:
        assert cancel is not None
        cancel.wait(10)
        given_up.append(cancel.is_set())
        raise auth.LoginCancelled("given up")

    monkeypatch.setattr(auth, "log_in", waits)
    app = make_app(make_cfg(tmp_path), tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.press("t")
        assert isinstance(app.screen, CTraderDialog)
        app.screen.query_one("#redirect").focus()
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app._ctrader_cancel is not None

        await pilot.press("t")
        assert isinstance(app.screen, CTraderDialog)
        for _ in range(50):
            await pilot.pause(0.02)
            if given_up:
                break
        assert given_up == [True]
        assert app._ctrader_cancel is None
