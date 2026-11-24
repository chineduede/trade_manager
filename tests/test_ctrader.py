"""cTrader: the Open API client's framing, the conversion to the copier's own types, the login,
the config's cTrader accounts, and a worker run end to end against a fake Open API server."""

from __future__ import annotations

import asyncio
import queue
import struct
import threading
import time
import urllib.error
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
    SizingBasis,
    Snapshot,
    TradeConstraint,
    WorkerState,
)

ACCOUNT = 4_200_001
EURUSD = cv.SymbolInfo(symbol_id=1, name="EURUSD", digits=5, lot_size=10_000_000)
USD, EUR, GBP, JPY, CHF = 15, 1, 2, 3, 4
_ASSETS = {USD: "USD", EUR: "EUR", GBP: "GBP", JPY: "JPY", CHF: "CHF"}
_SYMBOLS = {
    1: ("EURUSD", EUR, USD, 5),
    2: ("EURGBP", EUR, GBP, 5),
    3: ("USDJPY", USD, JPY, 3),
    4: ("GBPUSD", GBP, USD, 5),
    5: ("GBPJPY", GBP, JPY, 3),
    6: ("EURCHF", EUR, CHF, 5),
}
"""The fake server's symbols: name, base and quote asset, digits. The account is kept in USD."""
_CHAINS = {GBP: [4], JPY: [3]}
"""The symbols the fake server names to convert an asset into USD. CHF has none."""


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


def test_a_redirect_from_another_login_is_turned_away_and_the_wait_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    result: dict[str, Any] = {}

    def listen() -> None:
        result["code"] = auth.wait_for_code(redirect, timeout=10, state="ours")

    thread = threading.Thread(target=listen)
    with caplog.at_level("DEBUG", logger="mt5copier.ctrader.auth"):
        thread.start()
        deadline = time.monotonic() + 5
        while True:
            try:
                urllib.request.urlopen(f"{redirect}?code=THEIRS&state=theirs", timeout=2)
                raise AssertionError("a foreign state was accepted")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
                break
            except urllib.error.URLError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        urllib.request.urlopen(f"{redirect}?code=OURS&state=ours", timeout=2).read()
        thread.join(10)
    assert result["code"] == "OURS"
    assert any("sent back the login's state" in r.getMessage() for r in caplog.records)
    # The code is a credential while it lasts: the listener's log leaves the query out.
    assert not any("OURS" in r.getMessage() or "THEIRS" in r.getMessage() for r in caplog.records)


def test_a_redirect_without_a_state_is_taken() -> None:
    """cTrader's documentation does not promise to send the state back."""
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    result: dict[str, Any] = {}

    def listen() -> None:
        result["code"] = auth.wait_for_code(redirect, timeout=10, state="ours")

    thread = threading.Thread(target=listen)
    thread.start()
    deadline = time.monotonic() + 5
    while True:
        try:
            urllib.request.urlopen(f"{redirect}?code=XYZ", timeout=2).read()
            break
        except urllib.error.URLError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    thread.join(10)
    assert result["code"] == "XYZ"


def test_the_authorise_url_names_the_app_and_its_redirect() -> None:
    creds = auth.CTraderCredentials(client_id="123_abc", redirect_uri="http://127.0.0.1:9290/cb")
    url = auth.authorize_url(creds, "s1")
    assert url.startswith(auth.AUTHORIZE_URL)
    assert "client_id=123_abc" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A9290%2Fcb" in url
    assert "scope=trading" in url
    assert "state=s1" in url


def _secret_creds() -> auth.CTraderCredentials:
    return auth.CTraderCredentials(
        client_id="id",
        client_secret="the-client-secret",
        access_token="the-access-token",
        refresh_token="the-refresh-token",
        accounts=[auth.CTraderAccount(account_id=ACCOUNT, is_live=False, login=9, broker="X")],
    )


def test_credentials_round_trip_encrypted(tmp_path: Path) -> None:
    path = tmp_path / "ctrader.json"
    creds = _secret_creds()
    auth.save(creds, path)
    text = path.read_text(encoding="utf-8")
    assert '"format": "dpapi"' in text
    for secret in ("the-client-secret", "the-access-token", "the-refresh-token"):
        assert secret not in text
    again = auth.load(path)
    assert again == creds
    assert again.account(ACCOUNT) is not None


def test_plain_credentials_are_read_and_then_encrypted_once(tmp_path: Path) -> None:
    path = tmp_path / "ctrader.json"
    creds = _secret_creds()
    path.write_text(creds.model_dump_json(), encoding="utf-8")
    assert auth.load(path) == creds
    assert auth.encrypt_saved(path) is True
    assert "the-access-token" not in path.read_text(encoding="utf-8")
    assert auth.load(path) == creds
    assert auth.encrypt_saved(path) is False
    assert auth.encrypt_saved(tmp_path / "missing.json") is False


def test_credentials_encrypted_elsewhere_say_to_log_in_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "ctrader.json"
    path.write_text('{"format": "dpapi", "data": "bm90IGEgYmxvYg=="}', encoding="utf-8")
    with caplog.at_level("ERROR"):
        assert auth.load(path) == auth.CTraderCredentials()
    assert not path.exists()
    assert any("another machine" in r.getMessage() for r in caplog.records)


def test_a_corrupt_credentials_file_is_moved_aside_without_quoting_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "ctrader.json"
    path.write_text("{not json", encoding="utf-8")
    assert auth.load(path) == auth.CTraderCredentials()
    assert not path.exists()
    # A value refused by validation is named by its field, never quoted.
    path.write_text('{"access_token": ["the-access-token"]}', encoding="utf-8")
    with caplog.at_level("ERROR"):
        assert auth.load(path) == auth.CTraderCredentials()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "invalid access_token" in messages
    assert "the-access-token" not in messages


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

    def __init__(
        self, *, refuse_account: str | None = None, refuse_order: str | None = None
    ) -> None:
        self.refuse_account = refuse_account
        self.refuse_order = refuse_order
        self.seen: list[str] = []
        self.requests: list[Any] = []
        self.orders: dict[int, Any] = {}
        self.positions: dict[int, Any] = {
            77: om.ProtoOAPosition(
                positionId=77,
                tradeData=_trade(om.BUY, 5_000_000, label=cv.COPIER_LABEL),
                positionStatus=om.POSITION_STATUS_OPEN,
                swap=-150,
                price=1.1,
                usedMargin=2_500,
            )
        }
        self.prices: dict[int, tuple[int, int]] = {
            1: (110_050, 110_062),
            3: (12_500_000, 12_501_000),
            4: (125_000, 125_010),
        }
        """``(bid, ask)`` by symbol, in cTrader's 1/100000, sent on subscribing."""
        self.commission = 3.5
        """What each deal is charged, in dollars."""
        now = int(time.time() * 1000)
        hour = 3_600_000
        self.history: list[Any] = [
            om.ProtoOADeal(
                dealId=601,
                orderId=600,
                positionId=60,
                volume=100_000,
                filledVolume=100_000,
                symbolId=1,
                createTimestamp=now - 3 * hour,
                executionTimestamp=now - 3 * hour,
                tradeSide=om.BUY,
                dealStatus=om.FILLED,
                commission=-35_000,
                moneyDigits=4,
            ),
            om.ProtoOADeal(
                dealId=603,
                orderId=602,
                positionId=60,
                volume=100_000,
                filledVolume=100_000,
                symbolId=1,
                createTimestamp=now - 2 * hour,
                executionTimestamp=now - 2 * hour,
                tradeSide=om.SELL,
                dealStatus=om.FILLED,
                commission=-350,
                moneyDigits=2,
                closePositionDetail=om.ProtoOAClosePositionDetail(
                    entryPrice=1.1,
                    grossProfit=5_000,
                    swap=-100,
                    commission=-350,
                    balance=1_000_000,
                    moneyDigits=2,
                ),
            ),
            # Refused: it moved no money.
            om.ProtoOADeal(
                dealId=605,
                orderId=604,
                positionId=61,
                volume=100_000,
                filledVolume=0,
                symbolId=1,
                createTimestamp=now - hour,
                executionTimestamp=now - hour,
                tradeSide=om.BUY,
                dealStatus=om.REJECTED,
                commission=-35_000,
                moneyDigits=4,
            ),
        ]
        """The deal list: opened 3 hours ago, closed 2 hours ago for $45.50 net, and a refusal."""
        self.cash: list[Any] = [
            om.ProtoOADepositWithdraw(
                operationType=om.BALANCE_DEPOSIT,
                balanceHistoryId=1,
                balance=995_800,
                delta=995_800,
                changeBalanceTimestamp=now - 40 * hour,
                moneyDigits=2,
            )
        ]
        """The cash flows: a $9,958 deposit, which with the trade above leaves $10,000."""
        self.rows = 1000
        """How many deals one answer holds before it says there are more."""
        self.refuse_history = False
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
        self.requests.append(msg)
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
                ctidTraderAccountId=ACCOUNT,
                asset=[om.ProtoOAAsset(assetId=a, name=n) for a, n in _ASSETS.items()],
            )
        if isinstance(msg, oa.ProtoOASymbolsForConversionReq):
            assert msg.lastAssetId == USD
            return oa.ProtoOASymbolsForConversionRes(
                ctidTraderAccountId=ACCOUNT,
                symbol=[
                    om.ProtoOALightSymbol(
                        symbolId=sid,
                        symbolName=_SYMBOLS[sid][0],
                        baseAssetId=_SYMBOLS[sid][1],
                        quoteAssetId=_SYMBOLS[sid][2],
                    )
                    for sid in _CHAINS.get(msg.firstAssetId, [])
                ],
            )
        if isinstance(msg, oa.ProtoOASymbolsListReq):
            return oa.ProtoOASymbolsListRes(
                ctidTraderAccountId=ACCOUNT,
                symbol=[
                    om.ProtoOALightSymbol(
                        symbolId=sid, symbolName=n, baseAssetId=base, quoteAssetId=quote
                    )
                    for sid, (n, base, quote, _) in _SYMBOLS.items()
                ],
            )
        if isinstance(msg, oa.ProtoOASymbolByIdReq):
            return oa.ProtoOASymbolByIdRes(
                ctidTraderAccountId=ACCOUNT,
                symbol=[
                    om.ProtoOASymbol(
                        symbolId=sid,
                        digits=_SYMBOLS[sid][3],
                        pipPosition=_SYMBOLS[sid][3] - 1,
                        lotSize=10_000_000,
                        minVolume=100_000,
                        maxVolume=1_000_000_000,
                        stepVolume=100_000,
                    )
                    for sid in msg.symbolId
                ],
            )
        if isinstance(msg, oa.ProtoOAReconcileReq):
            return oa.ProtoOAReconcileRes(
                ctidTraderAccountId=ACCOUNT,
                position=list(self.positions.values()),
                order=list(self.orders.values()),
            )
        if isinstance(msg, oa.ProtoOASubscribeSpotsReq):
            spots = [
                oa.ProtoOASpotEvent(
                    ctidTraderAccountId=ACCOUNT,
                    symbolId=sid,
                    bid=self.prices[sid][0],
                    ask=self.prices[sid][1],
                )
                for sid in msg.symbolId
                if sid in self.prices
            ]
            if 1 in msg.symbolId:
                # Only the side that moved.
                spots.append(
                    oa.ProtoOASpotEvent(ctidTraderAccountId=ACCOUNT, symbolId=1, bid=110_055)
                )
            return [oa.ProtoOASubscribeSpotsRes(ctidTraderAccountId=ACCOUNT), *spots]
        if isinstance(msg, oa.ProtoOAAmendOrderReq):
            order = next((o for o in self.orders.values() if o.orderId == msg.orderId), None)
            if order is None:
                return self._order_error("ORDER_NOT_FOUND")
            for name in ("limitPrice", "stopPrice", "stopLoss", "takeProfit"):
                if msg.HasField(name):
                    setattr(order, name, getattr(msg, name))
            return self._execution(om.ORDER_REPLACED, self._shell(order), 0, order=order)
        if isinstance(msg, oa.ProtoOACancelOrderReq):
            order = next((o for o in self.orders.values() if o.orderId == msg.orderId), None)
            if order is None:
                return self._order_error("ORDER_NOT_FOUND")
            del self.orders[order.positionId]
            return self._execution(om.ORDER_CANCELLED, self._shell(order), 0, order=order)
        if isinstance(msg, oa.ProtoOANewOrderReq):
            return self._new_order(msg)
        if isinstance(msg, oa.ProtoOAExpectedMarginReq):
            # 1,000.00 a lot either way: a lot is 10,000,000 in cTrader's volume.
            return oa.ProtoOAExpectedMarginRes(
                ctidTraderAccountId=ACCOUNT,
                margin=[
                    om.ProtoOAExpectedMargin(volume=v, buyMargin=v // 100, sellMargin=v // 100)
                    for v in msg.volume
                ],
                moneyDigits=2,
            )
        if isinstance(msg, oa.ProtoOAClosePositionReq):
            return self._close(msg)
        if isinstance(msg, oa.ProtoOAAmendPositionSLTPReq):
            if msg.positionId not in self.positions:
                return self._order_error("POSITION_NOT_FOUND")
            pos = self.positions[msg.positionId]
            pos.stopLoss = msg.stopLoss
            pos.takeProfit = msg.takeProfit
            return self._execution(om.ORDER_ACCEPTED, pos, 900)
        if isinstance(msg, oa.ProtoOADealListReq):
            if self.refuse_history:
                return oa.ProtoOAErrorRes(errorCode="REQUEST_FREQUENCY_EXCEEDED", description="")
            found = [
                d
                for d in self.history
                if msg.fromTimestamp <= d.executionTimestamp < msg.toTimestamp
            ]
            return oa.ProtoOADealListRes(
                ctidTraderAccountId=ACCOUNT,
                deal=found[: self.rows],
                hasMore=len(found) > self.rows,
            )
        if isinstance(msg, oa.ProtoOACashFlowHistoryListReq):
            return oa.ProtoOACashFlowHistoryListRes(
                ctidTraderAccountId=ACCOUNT,
                depositWithdraw=[
                    c
                    for c in self.cash
                    if msg.fromTimestamp <= c.changeBalanceTimestamp < msg.toTimestamp
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

    def _order_error(self, code: str) -> Any:
        return oa.ProtoOAOrderErrorEvent(ctidTraderAccountId=ACCOUNT, errorCode=code)

    def _execution(
        self, kind: int, position: Any, order_id: int, deal: Any = None, order: Any = None
    ) -> Any:
        event = oa.ProtoOAExecutionEvent(
            ctidTraderAccountId=ACCOUNT,
            executionType=kind,  # type: ignore[arg-type]
            position=position,
            order=order
            or om.ProtoOAOrder(
                orderId=order_id,
                tradeData=position.tradeData,
                orderType=om.MARKET,
                orderStatus=om.ORDER_STATUS_ACCEPTED,
            ),
        )
        if deal is not None:
            event.deal.CopyFrom(deal)
        return event

    def _deal(self, order_id: int, position_id: int, volume: int, price: float) -> Any:
        return om.ProtoOADeal(
            dealId=order_id + 1,
            orderId=order_id,
            positionId=position_id,
            volume=volume,
            filledVolume=volume,
            symbolId=1,
            createTimestamp=1,
            executionTimestamp=1,
            executionPrice=price,
            tradeSide=om.BUY,
            dealStatus=om.FILLED,
            # $3.50, in the deal's own digits rather than the account's 2.
            commission=-round(self.commission * 10_000),
            moneyDigits=4,
        )

    def _shell(self, order: Any) -> Any:
        """The empty position cTrader creates for a resting order."""
        return om.ProtoOAPosition(
            positionId=order.positionId,
            tradeData=order.tradeData,
            positionStatus=om.POSITION_STATUS_CREATED,
            swap=0,
        )

    def _new_order(self, msg: Any) -> Any:
        if self.refuse_order:
            return self._order_error(self.refuse_order)
        if msg.orderType in (om.LIMIT, om.STOP):
            order = om.ProtoOAOrder(
                orderId=700,
                positionId=99,
                tradeData=om.ProtoOATradeData(
                    symbolId=msg.symbolId,
                    volume=msg.volume,
                    tradeSide=msg.tradeSide,
                    label=msg.label,
                    openTimestamp=1,
                ),
                orderType=msg.orderType,
                orderStatus=om.ORDER_STATUS_ACCEPTED,
            )
            for name in ("limitPrice", "stopPrice", "stopLoss", "takeProfit"):
                if msg.HasField(name):
                    setattr(order, name, getattr(msg, name))
            self.orders[99] = order
            return self._execution(om.ORDER_ACCEPTED, self._shell(order), 0, order=order)
        position = om.ProtoOAPosition(
            positionId=88,
            tradeData=om.ProtoOATradeData(
                symbolId=msg.symbolId,
                volume=msg.volume,
                tradeSide=msg.tradeSide,
                label=msg.label,
                openTimestamp=1,
            ),
            positionStatus=om.POSITION_STATUS_CREATED,
            swap=0,
            price=0.0,
        )
        accepted = self._execution(om.ORDER_ACCEPTED, position, 500)
        filled = om.ProtoOAPosition()
        filled.CopyFrom(position)
        filled.positionStatus = om.POSITION_STATUS_OPEN
        filled.price = 1.10012
        self.positions[88] = filled
        return [
            accepted,
            # Something unrelated in between, which must reach the worker untouched.
            oa.ProtoOAMarginChangedEvent(
                ctidTraderAccountId=ACCOUNT, positionId=77, usedMargin=1, moneyDigits=2
            ),
            self._execution(om.ORDER_FILLED, filled, 500, self._deal(500, 88, msg.volume, 1.10012)),
        ]

    def _close(self, msg: Any) -> Any:
        pos = self.positions.get(msg.positionId)
        if pos is None:
            return self._order_error("POSITION_NOT_FOUND")
        left = int(pos.tradeData.volume) - msg.volume
        if left <= 0:
            del self.positions[msg.positionId]
        else:
            pos.tradeData.volume = left
        return [
            self._execution(om.ORDER_ACCEPTED, pos, 600),
            self._execution(
                om.ORDER_FILLED, pos, 600, self._deal(600, msg.positionId, msg.volume, 1.1003)
            ),
        ]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readexactly(4)
                (length,) = struct.unpack(">I", head)
                msg, client_msg_id = decode(await reader.readexactly(length))
                if msg is None or client_msg_id is None:
                    continue
                answer = self._answer(msg)
                for each in answer if isinstance(answer, list) else [answer]:
                    writer.write(encode(each, client_msg_id))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()


def _creds() -> auth.CTraderCredentials:
    return auth.CTraderCredentials(client_id="i", client_secret="s", access_token="tok")


async def _run_worker(
    api: FakeOpenApi, commands: list[object], ticks: float = 1.2, is_live: bool = False
) -> list[object]:
    out_q: queue.Queue[object] = queue.Queue()
    cmd_q: queue.Queue[object] = queue.Queue()
    account = ct_worker._Account(
        ACCOUNT,
        is_live,
        "Broker-cTrader-Demo",
        lambda host: CTraderClient("127.0.0.1", api.port, tls=False),
        _creds,
    )
    task = asyncio.create_task(
        ct_worker._run("CTRADER:1", account, out_q, cmd_q, 0.05, [0.05])  # type: ignore[arg-type]
    )
    if commands:
        # A command that arrives before the connection is refused, as on MT5.
        for _ in range(100):
            if account.connected and account.trader is not None:
                break
            await asyncio.sleep(0.02)
        for c in commands:
            cmd_q.put(c)
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
    assert snap.deals is not None and [d.ticket for d in snap.deals] == [1, 601, 603]
    assert snap.deals_since is not None
    assert snap.open_positions is not None and len(snap.open_positions) == 1
    pos = snap.open_positions[0]
    assert (pos.ticket, pos.symbol, pos.volume, pos.magic) == (77, "EURUSD", 0.5, COPIER_MAGIC)
    assert snap.live_positions is not None
    assert snap.live_positions[0].swap == pytest.approx(-1.5)
    # A buy is priced at the bid, the side that moved last.
    assert snap.live_positions[0].price_current == pytest.approx(1.10055)
    assert "ProtoOASubscribeSpotsReq" in api.seen
    assert snap.pending_orders == ()
    assert isinstance(got[-1], Snapshot) and got[-1].state is WorkerState.STOPPED


def _command(op: ActionKind, **kw: Any) -> OrderCommand:
    defaults: dict[str, Any] = {
        "command_id": 3,
        "op": op,
        "master_ticket": 11,
        "master_symbol": "EURUSD",
        "direction": PositionType.BUY,
    }
    return OrderCommand(**(defaults | kw))


async def _results(
    api: FakeOpenApi, *cmds: OrderCommand, is_live: bool = False
) -> list[OrderResult]:
    await api.start()
    try:
        got = await _run_worker(api, list(cmds), ticks=0.4, is_live=is_live)
    finally:
        await api.stop()
    return [r for r in got if isinstance(r, OrderResult)]


def _sent(api: FakeOpenApi, kind: type) -> list[Any]:
    return [r for r in api.requests if isinstance(r, kind)]


def test_a_lot_is_valued_in_the_quote_or_through_the_symbols_own_price() -> None:
    eurusd = cv.SymbolInfo(1, "EURUSD", 5, 10_000_000, base_asset_id=EUR, quote_asset_id=USD)
    usdjpy = cv.SymbolInfo(3, "USDJPY", 3, 10_000_000, base_asset_id=USD, quote_asset_id=JPY)
    eurgbp = cv.SymbolInfo(2, "EURGBP", 5, 10_000_000, base_asset_id=EUR, quote_asset_id=GBP)
    # 10 pips on a standard lot of EURUSD is $100.
    assert cv.loss_per_lot(eurusd, USD, 1.1000, 1.0990) == pytest.approx(100.0)
    # 10 pips of USDJPY is 10,000 yen, worth $66.67 at a stop of 150.
    assert cv.loss_per_lot(usdjpy, USD, 150.10, 150.00) == pytest.approx(10_000 / 150.0)
    # A cross needs a third price.
    assert cv.loss_per_lot(eurgbp, USD, 0.85, 0.849) is None
    # 10 pips of EURGBP is 100 pounds, worth $125 when a pound is $1.25.
    assert cv.loss_per_lot(eurgbp, USD, 0.85, 0.849, 1.25) == pytest.approx(125.0)
    assert cv.is_cross(eurgbp, USD) and not cv.is_cross(usdjpy, USD)


def test_a_conversion_chain_is_walked_at_the_side_that_values_a_loss_high() -> None:
    gbpusd, usdjpy = cv.Link(4, GBP, USD), cv.Link(3, USD, JPY)
    spots: dict[int, tuple[float | None, float | None]] = {
        4: (1.2500, 1.2502),
        3: (150.00, 150.02),
    }
    # From a link's base: times its ask.
    assert cv.conversion_rate((gbpusd,), GBP, USD, spots) == pytest.approx(1.2502)
    # From its quote: divided by its bid.
    assert cv.conversion_rate((usdjpy,), JPY, USD, spots) == pytest.approx(1 / 150.00)
    # Two links, taken as they connect rather than as listed.
    assert cv.conversion_rate((gbpusd, usdjpy), JPY, GBP, spots) == pytest.approx(
        1 / 150.00 / 1.2500
    )
    assert cv.conversion_rate((), USD, USD, spots) == 1.0
    # No price, or a chain that leads elsewhere: no rate.
    assert cv.conversion_rate((gbpusd,), GBP, USD, {4: (1.25, None)}) is None
    assert cv.conversion_rate((gbpusd,), JPY, USD, spots) is None


def test_symbol_bounds_become_lots_and_distances_relative_units() -> None:
    sym = cv.SymbolInfo(1, "EURUSD", 5, 10_000_000, 100_000, 1_000_000_000, 100_000)
    facts = cv.facts(sym)
    assert (facts.volume_min, facts.volume_max, facts.volume_step) == (0.01, 100.0, 0.01)
    assert facts.point == pytest.approx(0.00001)
    assert sym.volume(0.3) == 3_000_000
    assert cv.relative(1.10000 - 1.09900, 5) == 100


async def test_an_open_is_risk_sized_filled_and_given_its_exact_stops() -> None:
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.OPEN, entry=1.1000, sl=1.0990, tp=1.1030, risk_amount=100.0, comment="cp"
    )
    results = await _results(api, cmd)
    assert len(results) == 1
    r = results[0]
    assert r.ok, r.error
    assert (r.slave_ticket, r.slave_symbol, r.filled_volume) == (88, "EURUSD", 1.0)
    assert r.price == pytest.approx(1.10012)
    assert r.point == pytest.approx(0.00001) and r.digits == 5
    assert r.price_value == pytest.approx(100_000.0)
    assert r.commission == pytest.approx(3.5)
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert order.orderType == om.MARKET and order.tradeSide == om.BUY
    assert order.volume == 10_000_000
    assert order.label == cv.COPIER_LABEL
    # Protected from the moment it fills, by distance; then exact.
    assert (order.relativeStopLoss, order.relativeTakeProfit) == (100, 300)
    (amend,) = _sent(api, oa.ProtoOAAmendPositionSLTPReq)
    assert (amend.positionId, amend.stopLoss, amend.takeProfit) == (88, 1.099, 1.103)


def _cross(symbol: str, entry: float, sl: float, risk: float, n: int = 0) -> OrderCommand:
    return _command(
        ActionKind.OPEN,
        command_id=3 + n,
        master_ticket=11 + n,
        master_symbol=symbol,
        entry=entry,
        sl=sl,
        risk_amount=risk,
    )


async def test_a_cross_is_risk_sized_through_its_conversion_chain() -> None:
    api = FakeOpenApi()
    results = await _results(api, _cross("EURGBP", 0.85, 0.849, 250))
    r = results[0]
    assert r.ok, r.error
    # 100 pounds a lot at the stop, at GBPUSD's ask of 1.2501: $125.01, so 1.99 lots.
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert order.volume == 19_900_000
    assert r.price_value == pytest.approx(100_000 * 1.2501)
    (chain,) = _sent(api, oa.ProtoOASymbolsForConversionReq)
    assert (chain.firstAssetId, chain.lastAssetId) == (GBP, USD)


async def test_a_cross_quoted_in_yen_divides_by_usdjpy() -> None:
    api = FakeOpenApi()
    results = await _results(api, _cross("GBPJPY", 190.00, 189.50, 1_000))
    assert results[0].ok, results[0].error
    # 50,000 yen a lot at the stop, at USDJPY's bid of 125: $400, so 2.5 lots.
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert order.volume == 25_000_000


async def test_a_chain_is_asked_for_once_per_currency() -> None:
    api = FakeOpenApi()
    results = await _results(
        api,
        _cross("EURGBP", 0.85, 0.849, 250),
        _cross("GBPUSD", 1.25, 1.249, 100, 1),
        _cross("EURGBP", 0.85, 0.849, 250, 2),
    )
    assert len(results) == 3 and all(r.ok for r in results), [r.error for r in results]
    assert len(_sent(api, oa.ProtoOASymbolsForConversionReq)) == 1


async def test_a_cross_with_no_way_to_convert_is_refused_before_anything_is_sent() -> None:
    api = FakeOpenApi()
    results = await _results(api, _cross("EURCHF", 0.95, 0.949, 50))
    assert results[0].ok is False
    assert results[0].error is not None and "no way to convert CHF to USD" in results[0].error
    assert not _sent(api, oa.ProtoOANewOrderReq)


async def test_a_cross_without_a_price_yet_is_refused_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ct_worker, "SPOT_WAIT_S", 0.1)
    api = FakeOpenApi()
    del api.prices[4]
    results = await _results(api, _cross("EURGBP", 0.85, 0.849, 50))
    assert results[0].ok is False
    assert results[0].error is not None and "no price for GBPUSD yet" in results[0].error
    assert not _sent(api, oa.ProtoOANewOrderReq)


async def test_a_lot_multiple_cross_goes_ahead_without_a_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ct_worker, "SPOT_WAIT_S", 0.1)
    api = FakeOpenApi()
    del api.prices[4]
    cmd = _command(
        ActionKind.OPEN,
        master_symbol="EURGBP",
        sizing_basis=SizingBasis.MASTER_LOT_MULTIPLE,
        master_volume=0.2,
    )
    results = await _results(api, cmd)
    assert results[0].ok, results[0].error
    assert results[0].price_value is None


async def test_a_lot_multiple_open_needs_no_stop() -> None:
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.OPEN,
        master_symbol="EURGBP",
        sizing_basis=SizingBasis.MASTER_LOT_MULTIPLE,
        master_volume=0.2,
        lot_multiple=1.5,
    )
    results = await _results(api, cmd)
    assert results[0].ok, results[0].error
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert order.volume == 3_000_000
    assert not order.HasField("relativeStopLoss")
    assert not _sent(api, oa.ProtoOAAmendPositionSLTPReq)


async def test_the_total_margin_cap_counts_what_the_account_has_open() -> None:
    # 2 lots need 2,000.00; of the 1,500.00 total, position 77 uses 25.00 already.
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.OPEN,
        sizing_basis=SizingBasis.MASTER_LOT_MULTIPLE,
        master_volume=2.0,
        lot_multiple=1.0,
        constraints=(TradeConstraint("total_margin", 1_500.0, "shrink", "Total margin in use"),),
    )
    results = await _results(api, cmd)
    assert results[0].ok, results[0].error
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert order.volume == 14_700_000
    [note] = results[0].guard
    assert note.text() == (
        "Total margin in use shrunk it: from 2 to 1.47 lots: 2,000.00 margin needed, "
        "1,475.00 left of the 1,500.00 total (25.00 in use)"
    )


async def test_the_margin_per_trade_cap_on_ctrader() -> None:
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.OPEN,
        sizing_basis=SizingBasis.MASTER_LOT_MULTIPLE,
        master_volume=2.0,
        lot_multiple=1.0,
        constraints=(TradeConstraint("margin", 500.0, "decline"),),
    )
    results = await _results(api, cmd)
    assert not results[0].ok
    assert not _sent(api, oa.ProtoOANewOrderReq)
    assert [n.action for n in results[0].guard] == ["declined"]


async def test_a_fill_charged_nothing_reports_no_commission() -> None:
    api = FakeOpenApi()
    api.commission = 0.0
    results = await _results(api, _command(ActionKind.OPEN, entry=1.1, sl=1.099, risk_amount=100))
    assert results[0].ok, results[0].error
    assert results[0].commission is None


async def test_a_refused_order_is_a_final_failure() -> None:
    api = FakeOpenApi(refuse_order="NOT_ENOUGH_MONEY")
    results = await _results(api, _command(ActionKind.OPEN, entry=1.1, sl=1.099, risk_amount=100))
    assert results[0].ok is False and results[0].retryable is False
    assert results[0].error is not None and "NOT_ENOUGH_MONEY" in results[0].error


async def test_a_close_takes_the_volume_ctrader_holds() -> None:
    api = FakeOpenApi()
    results = await _results(api, _command(ActionKind.CLOSE, slave_ticket=77))
    assert results[0].ok, results[0].error
    assert results[0].filled_volume == 0.5
    (close,) = _sent(api, oa.ProtoOAClosePositionReq)
    assert (close.positionId, close.volume) == (77, 5_000_000)
    assert 77 not in api.positions


async def test_a_reduce_closes_its_fraction() -> None:
    api = FakeOpenApi()
    results = await _results(api, _command(ActionKind.REDUCE, slave_ticket=77, close_fraction=0.4))
    assert results[0].ok, results[0].error
    (close,) = _sent(api, oa.ProtoOAClosePositionReq)
    assert close.volume == 2_000_000


async def test_closing_a_position_already_gone_is_done() -> None:
    api = FakeOpenApi()
    results = await _results(api, _command(ActionKind.CLOSE, slave_ticket=12345))
    assert results[0].ok and results[0].outcome == "already closed"
    assert not _sent(api, oa.ProtoOAClosePositionReq)


async def test_stops_follow_the_master_and_a_zero_clears_one() -> None:
    api = FakeOpenApi()
    results = await _results(
        api, _command(ActionKind.MODIFY_SLTP, slave_ticket=77, sl=1.095, tp=0.0)
    )
    assert results[0].ok, results[0].error
    (amend,) = _sent(api, oa.ProtoOAAmendPositionSLTPReq)
    assert amend.stopLoss == 1.095
    assert not amend.HasField("takeProfit")


async def test_a_direct_open_lands_its_target_past_the_rr_by_the_commissions() -> None:
    """Entry 1.1000, stop 1.0990 ($100 a lot, $100,000 per 1.0), target 1.1030 is rr 3: a $5
    round turn pads the exact amend's target by (3 + 1) x 5 / 100,000 = 0.0002."""
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.OPEN,
        entry=1.1000,
        sl=1.0990,
        tp=1.1030,
        risk_amount=100.0,
        tp_net_rr=True,
        commission_per_lot=5.0,
    )
    results = await _results(api, cmd)
    assert results[0].ok, results[0].error
    (amend,) = _sent(api, oa.ProtoOAAmendPositionSLTPReq)
    assert amend.takeProfit == pytest.approx(1.1032)
    assert amend.stopLoss == pytest.approx(1.0990), "only the target moves"


async def test_a_direct_stop_move_restates_the_padded_target() -> None:
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.MODIFY_SLTP,
        slave_ticket=77,
        entry=1.1000,
        sl=1.0990,
        tp=1.1030,
        tp_net_rr=True,
        commission_per_lot=5.0,
        risk_distance=0.0010,
    )
    results = await _results(api, cmd)
    assert results[0].ok, results[0].error
    (amend,) = _sent(api, oa.ProtoOAAmendPositionSLTPReq)
    assert amend.takeProfit == pytest.approx(1.1032)


async def test_a_stop_at_breakeven_keeps_the_padded_target() -> None:
    """A stop moved to entry measures zero risk; the latched one-R distance is what the pad
    is measured and valued from, so the target stays past the ticket's level."""
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.MODIFY_SLTP,
        slave_ticket=77,
        entry=1.1000,
        sl=1.1000,
        tp=1.1030,
        tp_net_rr=True,
        commission_per_lot=5.0,
        risk_distance=0.0010,
    )
    results = await _results(api, cmd)
    assert results[0].ok, results[0].error
    (amend,) = _sent(api, oa.ProtoOAAmendPositionSLTPReq)
    assert amend.takeProfit == pytest.approx(1.1032)


async def test_a_stop_move_resends_the_resting_padded_target_verbatim() -> None:
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.MODIFY_SLTP,
        slave_ticket=77,
        entry=1.1000,
        sl=1.1000,
        tp=1.1030,
        tp_net_rr=True,
        commission_per_lot=5.0,
        padded_tp=1.1032,
    )
    results = await _results(api, cmd)
    assert results[0].ok, results[0].error
    (amend,) = _sent(api, oa.ProtoOAAmendPositionSLTPReq)
    assert amend.takeProfit == pytest.approx(1.1032)
    assert results[0].padded_tp == pytest.approx(1.1032)


async def test_a_stop_past_entry_with_no_latched_risk_pads_nothing() -> None:
    api = FakeOpenApi()
    cmd = _command(
        ActionKind.MODIFY_SLTP,
        slave_ticket=77,
        entry=1.1000,
        sl=1.1005,
        tp=1.1030,
        tp_net_rr=True,
        commission_per_lot=5.0,
    )
    results = await _results(api, cmd)
    assert results[0].ok, results[0].error
    (amend,) = _sent(api, oa.ProtoOAAmendPositionSLTPReq)
    assert amend.takeProfit == pytest.approx(1.1030)


async def test_moving_the_stops_of_a_gone_copy_says_so() -> None:
    api = FakeOpenApi()
    results = await _results(api, _command(ActionKind.MODIFY_SLTP, slave_ticket=999, sl=1.09))
    assert results[0].ok and results[0].target_gone


def _resting_order(api: FakeOpenApi) -> None:
    """A copy resting as a buy limit, as ``_new_order`` would leave one."""
    api.orders[99] = om.ProtoOAOrder(
        orderId=700,
        positionId=99,
        tradeData=_trade(om.BUY, 2_000_000, label=cv.COPIER_LABEL),
        orderType=om.LIMIT,
        orderStatus=om.ORDER_STATUS_ACCEPTED,
        limitPrice=1.09,
        stopLoss=1.085,
        takeProfit=1.1,
    )


def test_a_resting_order_is_known_by_the_position_it_will_become() -> None:
    api = FakeOpenApi()
    _resting_order(api)
    order = cv.pending_order(api.orders[99], EURUSD)
    assert order is not None and order.ticket == 99
    without = om.ProtoOAOrder()
    without.CopyFrom(api.orders[99])
    without.ClearField("positionId")
    assert cv.order_ticket(without) == 700


async def test_a_limit_is_placed_at_the_masters_levels_and_known_by_its_position() -> None:
    api = FakeOpenApi()
    place = _command(
        ActionKind.PLACE,
        order_type=OrderType.BUY_LIMIT,
        entry=1.0900,
        sl=1.0880,
        tp=1.0950,
        risk_amount=100.0,
    )
    (result,) = await _results(api, place)
    assert result.ok, result.error
    assert result.slave_ticket == 99
    assert result.filled_volume == 0.5
    assert result.price == 1.09 and result.digits == 5
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert order.orderType == om.LIMIT and order.tradeSide == om.BUY
    assert (order.limitPrice, order.stopLoss, order.takeProfit) == (1.09, 1.088, 1.095)
    assert not order.HasField("stopPrice")
    assert order.volume == 5_000_000


async def test_a_sell_stop_rests_at_its_stop_price() -> None:
    api = FakeOpenApi()
    place = _command(
        ActionKind.PLACE,
        order_type=OrderType.SELL_STOP,
        direction=PositionType.SELL,
        entry=1.0900,
        sl=1.0920,
        risk_amount=100.0,
    )
    (result,) = await _results(api, place)
    assert result.ok, result.error
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert order.orderType == om.STOP and order.tradeSide == om.SELL
    assert order.stopPrice == 1.09 and not order.HasField("limitPrice")


async def test_a_place_mirrors_the_masters_expiry_as_good_till_date() -> None:
    api = FakeOpenApi()
    place = _command(
        ActionKind.PLACE,
        order_type=OrderType.BUY_LIMIT,
        entry=1.0900,
        sl=1.0880,
        risk_amount=100.0,
        expiration_utc=1_780_000_000,
    )
    (result,) = await _results(api, place)
    assert result.ok, result.error
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert order.timeInForce == om.GOOD_TILL_DATE
    assert order.expirationTimestamp == 1_780_000_000_000, "cTrader takes UTC milliseconds"


async def test_a_place_with_no_expiry_stays_good_till_cancelled() -> None:
    api = FakeOpenApi()
    place = _command(
        ActionKind.PLACE,
        order_type=OrderType.BUY_LIMIT,
        entry=1.0900,
        sl=1.0880,
        risk_amount=100.0,
    )
    (result,) = await _results(api, place)
    assert result.ok, result.error
    (order,) = _sent(api, oa.ProtoOANewOrderReq)
    assert not order.HasField("expirationTimestamp")


def _resting_gtd_order(api: FakeOpenApi) -> None:
    """The same resting copy, carrying an expiry."""
    _resting_order(api)
    api.orders[99].timeInForce = om.GOOD_TILL_DATE
    api.orders[99].expirationTimestamp = 1_780_000_000_000


async def test_a_masters_expiry_change_alone_moves_the_resting_copy() -> None:
    api = FakeOpenApi()
    _resting_gtd_order(api)
    move = _command(
        ActionKind.MODIFY_ORDER,
        order_type=OrderType.BUY_LIMIT,
        slave_ticket=99,
        entry=1.09,
        sl=1.085,
        tp=1.1,
        expiration_utc=1_790_000_000,
    )
    (result,) = await _results(api, move)
    assert result.ok, result.error
    (amend,) = _sent(api, oa.ProtoOAAmendOrderReq)
    assert amend.expirationTimestamp == 1_790_000_000_000


async def test_an_expiry_ctrader_cannot_grant_still_counts_as_matched() -> None:
    """The amend message carries no time-in-force, so a GTC copy cannot become GTD; saying
    so and settling beats asking the server for the impossible on every tick."""
    api = FakeOpenApi()
    _resting_order(api)  # rests GTC
    move = _command(
        ActionKind.MODIFY_ORDER,
        order_type=OrderType.BUY_LIMIT,
        slave_ticket=99,
        entry=1.09,
        sl=1.085,
        tp=1.1,
        expiration_utc=1_790_000_000,
    )
    (result,) = await _results(api, move)
    assert result.ok, result.error
    assert not _sent(api, oa.ProtoOAAmendOrderReq)


def test_a_resting_gtd_order_reports_its_expiry_in_utc_seconds() -> None:
    api = FakeOpenApi()
    _resting_gtd_order(api)
    order = cv.pending_order(api.orders[99], EURUSD)
    assert order is not None and order.expiration_utc == 1_780_000_000
    api.orders[99].timeInForce = om.GOOD_TILL_CANCEL
    gtc = cv.pending_order(api.orders[99], EURUSD)
    assert gtc is not None and gtc.expiration_utc == 0


async def test_a_resting_copy_is_moved_by_its_order_id() -> None:
    api = FakeOpenApi()
    _resting_order(api)
    move = _command(
        ActionKind.MODIFY_ORDER,
        order_type=OrderType.BUY_LIMIT,
        slave_ticket=99,
        entry=1.0910,
        sl=1.0890,
        tp=1.1,
    )
    (result,) = await _results(api, move)
    assert result.ok, result.error
    (amend,) = _sent(api, oa.ProtoOAAmendOrderReq)
    assert amend.orderId == 700
    assert (amend.limitPrice, amend.stopLoss, amend.takeProfit) == (1.091, 1.089, 1.1)


async def test_a_resting_copy_already_where_the_master_is_is_not_sent() -> None:
    api = FakeOpenApi()
    _resting_order(api)
    same = _command(ActionKind.MODIFY_ORDER, slave_ticket=99, entry=1.09, sl=1.085, tp=1.1)
    (result,) = await _results(api, same)
    assert result.ok and result.outcome == "already matched"
    assert not _sent(api, oa.ProtoOAAmendOrderReq)


async def test_moving_a_copy_no_longer_resting_says_it_is_gone() -> None:
    api = FakeOpenApi()
    (result,) = await _results(api, _command(ActionKind.MODIFY_ORDER, slave_ticket=99, entry=1.0))
    assert result.ok and result.target_gone


async def test_a_resting_copy_is_cancelled_and_one_already_gone_is_done() -> None:
    api = FakeOpenApi()
    _resting_order(api)
    (result,) = await _results(api, _command(ActionKind.CANCEL, slave_ticket=99))
    assert result.ok, result.error
    (cancel,) = _sent(api, oa.ProtoOACancelOrderReq)
    assert cancel.orderId == 700
    assert not api.orders

    api = FakeOpenApi()
    (result,) = await _results(api, _command(ActionKind.CANCEL, slave_ticket=99))
    assert result.ok and result.outcome == "already gone"
    assert not _sent(api, oa.ProtoOACancelOrderReq)


async def test_a_live_account_is_refused() -> None:
    api = FakeOpenApi()
    (result,) = await _results(api, _command(ActionKind.CLOSE, slave_ticket=77), is_live=True)
    assert result.ok is False and result.error is not None and "not a demo" in result.error
    assert not _sent(api, oa.ProtoOAClosePositionReq)


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
            await client.request(oa.ProtoOAVersionReq())
    finally:
        await client.close()
        await api.stop()


# --- the browser login flow -------------------------------------------------------------------


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


# --- the deal history, for the guards ------------------------------------------------------------


def test_each_kind_of_entry_adds_what_it_moved_the_balance_by() -> None:
    api = FakeOpenApi()
    opened, closed, refused = api.history
    (deposit,) = api.cash

    entry = cv.booked_deal(opened, "EURUSD", 2)
    assert entry is not None and entry.balance_after is None
    assert (entry.deal.entry, entry.deal.net) == (0, pytest.approx(-3.5))

    entry = cv.booked_deal(closed, "EURUSD", 2)
    assert entry is not None and entry.balance_after == pytest.approx(10_000.0)
    deal = entry.deal
    assert (deal.ticket, deal.type, deal.entry, deal.symbol) == (603, 1, 1, "EURUSD")
    assert deal.position == 60
    assert (deal.profit, deal.swap, deal.commission) == (50.0, -1.0, -3.5)
    assert deal.net == pytest.approx(45.5)
    assert deal.time == pytest.approx(closed.executionTimestamp / 1000)

    assert cv.booked_deal(refused, "EURUSD", 2) is None

    cash = cv.booked_cash(deposit, 2)
    assert (cash.deal.type, cash.deal.net, cash.balance_after) == (2, 9_958.0, 9_958.0)
    out = om.ProtoOADepositWithdraw()
    out.CopyFrom(deposit)
    out.operationType = om.BALANCE_WITHDRAW
    out.delta = 20_000
    assert cv.booked_cash(out, 2).deal.net == pytest.approx(-200.0)


def test_the_history_is_checked_against_the_balance_ctrader_says_it_left() -> None:
    api = FakeOpenApi()
    history = [b for b in (cv.booked_deal(d, "EURUSD", 2) for d in api.history) if b]
    history.append(cv.booked_cash(api.cash[0], 2))
    assert cv.balance_mismatch(history, 10_000.0) is None
    said = cv.balance_mismatch(history, 10_010.0)
    assert said is not None and "10,000.00 after #603" in said and "10,010.00" in said
    assert cv.balance_mismatch([], 10_000.0) is None


def _connected(got: list[object]) -> Snapshot:
    live = [s for s in got if isinstance(s, Snapshot) and s.state is WorkerState.CONNECTED]
    assert live, [getattr(s, "error", None) for s in got]
    return live[-1]


async def test_a_guard_rebuilds_the_days_start_from_a_ctrader_history() -> None:
    from mt5copier.guards.account import AccountView

    api = FakeOpenApi()
    await api.start()
    try:
        snap = _connected(await _run_worker(api, []))
    finally:
        await api.stop()
    assert "ProtoOACashFlowHistoryListReq" in api.seen
    view = AccountView.of(snap, now=time.time(), initial_balance=None)
    # Before the close, and after the opening commission.
    assert view.balance_at(time.time() - 2.5 * 3600) == pytest.approx(9_954.5)
    assert view.balance_at(time.time() - 4 * 3600) == pytest.approx(9_958.0)
    assert view.balance_at(time.time() - 60 * 3600) is None, "before the window: unknown"


async def test_a_long_deal_list_is_asked_for_again_in_halves() -> None:
    api = FakeOpenApi()
    api.rows = 1
    await api.start()
    try:
        snap = _connected(await _run_worker(api, []))
    finally:
        await api.stop()
    assert snap.deals is not None and [d.ticket for d in snap.deals] == [1, 601, 603]
    asked = [m for m in api.requests if isinstance(m, oa.ProtoOADealListReq)]
    assert len(asked) > 2


async def test_a_history_that_cannot_be_read_whole_is_unknown_not_short() -> None:
    api = FakeOpenApi()
    api.refuse_history = True
    await api.start()
    try:
        snap = _connected(await _run_worker(api, []))
    finally:
        await api.stop()
    assert snap.deals is None and snap.deals_since is None
    assert snap.balance == pytest.approx(10_000.0), "the rest of the account is still read"
