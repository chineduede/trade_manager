"""One cTrader account's connection, owned by one OS process: the cTrader twin of
:mod:`mt5copier.worker`.

It speaks the same messages to the supervisor -- a :class:`Snapshot` every tick, an
:class:`OrderResult` for every :class:`OrderCommand` -- so the engine, the reconciler and the UI
treat the account like any other slave. Underneath it is an asyncio loop around one
:class:`CTraderClient`, re-reading the account's positions and orders each tick.

**It does not trade yet.** Every order command is refused with a final (not retryable) failure
that says so, which is what lets an account be followed in a dry run, and makes certain nothing
reaches the broker before sending is built and tested.

What a snapshot leaves unknown (None), and why:

* ``deals`` -- deposits and withdrawals come from a different request than trades, and a guard
  rebuilding a balance from half the history would be wrong rather than blank.
* a position's current price -- that needs a price subscription per symbol; its P/L is known.
"""

from __future__ import annotations

import asyncio
import os
import queue
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging_setup import configure, get_logger
from ..protocol import (
    ACCOUNT_TRADE_MODE_DEMO,
    Command,
    CommandKind,
    OrderCommand,
    OrderResult,
    PendingOrder,
    Position,
    PositionLive,
    Snapshot,
    WorkerState,
)
from . import auth
from . import convert as cv
from .client import CTraderClient, OpenApiError, host_for
from .proto import OpenApiMessages_pb2 as oa

if TYPE_CHECKING:
    from multiprocessing.queues import Queue

log = get_logger(__name__)

ACCOUNT_TRADE_MODE_REAL = 2
"""MT5's ``trade_mode`` for a real account, which a live cTrader account is reported as."""

_MIN_INTERVAL_S = 0.5
"""A tick here is three or four requests; the Open API allows 50 a second per connection."""

_TRADER_EVERY_S = 2.0
"""How often the balance is re-read when nothing has happened. An execution re-reads it at once."""

NOT_TRADING_YET = (
    "trading on cTrader is not built yet: this account can only be followed in a dry run"
)

_TOKEN_CODES = frozenset({"CH_ACCESS_TOKEN_INVALID", "OA_AUTH_TOKEN_EXPIRED", "ACCESS_DENIED"})

_EVENT_DISCONNECTS = (
    oa.ProtoOAAccountsTokenInvalidatedEvent,
    oa.ProtoOAClientDisconnectEvent,
    oa.ProtoOAAccountDisconnectEvent,
)


def _backoff_delay(schedule: list[float], attempt: int) -> float:
    if not schedule:
        return 5.0
    return schedule[min(attempt, len(schedule)) - 1]


class _Account:
    """The connection and what has been read through it."""

    def __init__(
        self,
        account_id: int,
        is_live: bool,
        server: str,
        client_factory: Callable[[str], CTraderClient],
        creds_loader: Callable[[], auth.CTraderCredentials],
    ) -> None:
        self.account_id = account_id
        self.is_live = is_live
        self.server = server
        """Fixed by the config (``config.ctrader_server_name``) rather than rebuilt from what the
        server reports, so the account's key -- and so its name and guards -- never changes."""
        self._client_factory = client_factory
        self._creds_loader = creds_loader
        self.client: CTraderClient | None = None
        self.trader: Any = None
        self.currency: str | None = None
        self.symbol_names: dict[int, str] = {}
        self.symbols: dict[int, cv.SymbolInfo] = {}
        self._trader_read_at = 0.0
        self.error: str | None = None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    async def connect(self) -> tuple[bool, str | None]:
        creds = self._creds_loader()
        if not creds.has_app:
            return False, "the cTrader app is not set up: press t on the Map"
        if not creds.logged_in:
            return False, "not logged in to cTrader: press t on the Map"
        client = self._client_factory(host_for(self.is_live))
        try:
            await client.connect()
            await client.request(
                oa.ProtoOAApplicationAuthReq(
                    clientId=creds.client_id, clientSecret=creds.client_secret
                )
            )
            await client.request(
                oa.ProtoOAAccountAuthReq(
                    ctidTraderAccountId=self.account_id, accessToken=creds.access_token
                )
            )
            self.client = client
            await self._read_trader()
            assets = await client.request(
                oa.ProtoOAAssetListReq(ctidTraderAccountId=self.account_id)
            )
            deposit = int(self.trader.depositAssetId)
            self.currency = next(
                (a.name for a in assets.asset if int(a.assetId) == deposit),
                None,
            )
            listed = await client.request(
                oa.ProtoOASymbolsListReq(ctidTraderAccountId=self.account_id)
            )
            self.symbol_names = {int(s.symbolId): s.symbolName for s in listed.symbol}
        except OpenApiError as exc:
            await self.disconnect()
            await client.close()
            if exc.code in _TOKEN_CODES:
                return (
                    False,
                    f"the cTrader login has expired or was withdrawn ({exc.code}): press t",
                )
            return False, f"cTrader refused: {exc}"
        except (OSError, TimeoutError, ConnectionError) as exc:
            await self.disconnect()
            await client.close()
            return False, f"could not reach cTrader ({exc!r})"
        return True, None

    async def disconnect(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            await client.close()

    async def _read_trader(self) -> None:
        assert self.client is not None
        answer = await self.client.request(oa.ProtoOATraderReq(ctidTraderAccountId=self.account_id))
        self.trader = answer.trader
        self._trader_read_at = time.monotonic()

    async def _symbols_for(self, ids: set[int]) -> None:
        missing = sorted(i for i in ids if i not in self.symbols)
        if not missing:
            return
        assert self.client is not None
        answer = await self.client.request(
            oa.ProtoOASymbolByIdReq(ctidTraderAccountId=self.account_id, symbolId=missing)
        )
        for s in answer.symbol:
            sid = int(s.symbolId)
            self.symbols[sid] = cv.SymbolInfo(
                symbol_id=sid,
                name=self.symbol_names.get(sid, f"#{sid}"),
                digits=int(s.digits),
                lot_size=int(s.lotSize),
            )

    def _drain_events(self) -> bool:
        """Read what the server sent unasked. True if an execution happened."""
        assert self.client is not None
        executed = False
        while True:
            try:
                event = self.client.events.get_nowait()
            except asyncio.QueueEmpty:
                return executed
            if isinstance(event, _EVENT_DISCONNECTS):
                reason = getattr(event, "reason", "") or type(event).__name__
                raise ConnectionError(f"cTrader ended the session: {reason}")
            if isinstance(event, oa.ProtoOAExecutionEvent):
                executed = True

    async def poll(self, terminal_id: str, seq: int, attempts: int) -> Snapshot:
        try:
            return await self._poll(terminal_id, seq, attempts)
        except OpenApiError as exc:
            self.error = f"cTrader refused a read: {exc}"
            log.warning("%s", self.error)
            return self._snapshot(terminal_id, seq, attempts, None, None, None)
        except (OSError, TimeoutError, ConnectionError) as exc:
            self.error = f"lost the connection to cTrader ({exc})"
            log.warning("%s", self.error)
            await self.disconnect()
            return Snapshot(
                terminal_id=terminal_id,
                state=WorkerState.DISCONNECTED,
                seq=seq,
                error=self.error,
                connect_attempts=attempts,
            )

    async def _poll(self, terminal_id: str, seq: int, attempts: int) -> Snapshot:
        assert self.client is not None
        if not self.client.connected:
            raise ConnectionError(self.client.close_reason or "closed")
        executed = self._drain_events()
        if executed or time.monotonic() - self._trader_read_at >= _TRADER_EVERY_S:
            await self._read_trader()
        state = await self.client.request(
            oa.ProtoOAReconcileReq(ctidTraderAccountId=self.account_id)
        )
        raw_positions = list(state.position)
        raw_orders = list(state.order)
        await self._symbols_for(
            {int(p.tradeData.symbolId) for p in raw_positions}
            | {int(o.tradeData.symbolId) for o in raw_orders}
        )
        pnl: dict[int, float] = {}
        if raw_positions:
            answer = await self.client.request(
                oa.ProtoOAGetPositionUnrealizedPnLReq(ctidTraderAccountId=self.account_id)
            )
            digits = int(answer.moneyDigits)
            pnl = {
                int(p.positionId): cv.money(int(p.netUnrealizedPnL), digits)
                for p in answer.positionUnrealizedPnL
            }
        positions = tuple(
            cv.position(p, self.symbols[int(p.tradeData.symbolId)]) for p in raw_positions
        )
        live = tuple(
            cv.position_live(p, pnl.get(int(p.positionId)), self._money_digits)
            for p in raw_positions
        )
        orders = tuple(
            o
            for o in (
                cv.pending_order(r, self.symbols[int(r.tradeData.symbolId)]) for r in raw_orders
            )
            if o is not None
        )
        used = sum(int(p.usedMargin) for p in raw_positions)
        self.error = None
        return self._snapshot(
            terminal_id,
            seq,
            attempts,
            (positions, live, orders),
            sum(pnl.values()),
            cv.money(used, self._money_digits),
        )

    @property
    def _money_digits(self) -> int:
        return int(self.trader.moneyDigits) if self.trader is not None else 2

    def _snapshot(
        self,
        terminal_id: str,
        seq: int,
        attempts: int,
        book: tuple[tuple[Position, ...], tuple[PositionLive, ...], tuple[PendingOrder, ...]]
        | None,
        profit: float | None,
        margin: float | None,
    ) -> Snapshot:
        trader = self.trader
        balance = cv.money(int(trader.balance), self._money_digits) if trader else None
        equity = balance + profit if balance is not None and profit is not None else None
        positions, live, orders = book if book is not None else (None, None, None)
        broker = str(trader.brokerName) if trader is not None else ""
        return Snapshot(
            terminal_id=terminal_id,
            state=WorkerState.CONNECTED,
            seq=seq,
            login=int(trader.traderLogin) if trader is not None else None,
            server=self.server,
            company=broker or None,
            currency=self.currency,
            balance=balance,
            equity=equity,
            margin=margin,
            margin_free=equity - margin if equity is not None and margin is not None else None,
            margin_level=(equity / margin * 100.0) if equity is not None and margin else 0.0,
            profit=profit,
            leverage=int(trader.leverageInCents) // 100 if trader is not None else None,
            # FULL_ACCESS is 0; anything else forbids opening trades.
            trade_allowed=int(trader.accessRights) == 0 if trader is not None else None,
            terminal_connected=True,
            positions=len(positions) if positions is not None else None,
            open_positions=positions,
            live_positions=live,
            pending_orders=orders,
            server_offset_s=0,
            deals=None,
            error=self.error,
            connect_attempts=attempts,
            trade_mode=ACCOUNT_TRADE_MODE_REAL if self.is_live else ACCOUNT_TRADE_MODE_DEMO,
        )


def refusal(terminal_id: str, cmd: OrderCommand) -> OrderResult:
    """The answer to every order command until sending is built."""
    return OrderResult(
        command_id=cmd.command_id,
        terminal_id=terminal_id,
        op=cmd.op,
        master_ticket=cmd.master_ticket,
        ok=False,
        retryable=False,
        error=NOT_TRADING_YET,
    )


def run_ctrader_worker(
    terminal_id: str,
    account_id: int,
    is_live: bool,
    server: str,
    out_q: Queue,
    cmd_q: Queue,
    log_file: str,
    poll_interval_ms: int = 250,
    reconnect_backoff_s: list[float] | None = None,
    _client_factory: Callable[[str], CTraderClient] | None = None,
    _creds_loader: Callable[[], auth.CTraderCredentials] | None = None,
) -> None:
    """Worker process entry point; module-level so the *spawn* start method can find it.

    ``_client_factory`` and ``_creds_loader`` are test seams."""
    configure(Path(log_file))
    asyncio.run(
        _run(
            terminal_id,
            _Account(
                account_id,
                is_live,
                server,
                _client_factory or CTraderClient,
                _creds_loader or auth.load,
            ),
            out_q,
            cmd_q,
            max(poll_interval_ms / 1000.0, _MIN_INTERVAL_S),
            reconnect_backoff_s or [1.0, 2.0, 5.0, 10.0, 30.0],
        )
    )


async def _run(
    terminal_id: str,
    account: _Account,
    out_q: Queue,
    cmd_q: Queue,
    interval: float,
    backoff: list[float],
) -> None:
    wlog = get_logger(f"worker.{terminal_id[:16]}")
    pid = os.getpid()
    seq = 0
    attempts = 0
    next_retry_at = 0.0
    running = True

    def publish(snap: Snapshot) -> None:
        try:
            out_q.put_nowait(replace(snap, worker_pid=pid))
        except Exception as exc:
            wlog.debug("could not publish snapshot: %r", exc)

    def publish_result(result: OrderResult) -> None:
        try:
            out_q.put(result, timeout=2.0)
        except Exception as exc:
            wlog.error("could not report order result for #%s: %r", result.master_ticket, exc)

    wlog.info("cTrader worker starting for account %s", account.account_id)
    publish(Snapshot(terminal_id=terminal_id, state=WorkerState.STARTING, seq=seq))
    try:
        while running:
            while True:
                try:
                    cmd = cmd_q.get_nowait()
                except queue.Empty:
                    break
                except (EOFError, OSError):
                    running = False
                    break
                if cmd is None or getattr(cmd, "kind", None) is CommandKind.STOP:
                    wlog.info("stop requested")
                    running = False
                    break
                if isinstance(cmd, OrderCommand):
                    result = refusal(terminal_id, cmd)
                    wlog.info("%s", result.describe())
                    publish_result(result)
                    continue
                if isinstance(cmd, Command) and cmd.kind is CommandKind.RECONNECT:
                    wlog.info("reconnect requested")
                    await account.disconnect()
                    attempts = 0
                    next_retry_at = 0.0
            if not running:
                break

            if not account.connected:
                now = time.monotonic()
                if now < next_retry_at:
                    seq += 1
                    publish(
                        Snapshot(
                            terminal_id=terminal_id,
                            state=WorkerState.CONNECTING,
                            seq=seq,
                            error=account.error or f"retrying in {next_retry_at - now:.0f}s",
                            connect_attempts=attempts,
                        )
                    )
                    await asyncio.sleep(min(interval, max(next_retry_at - now, 0.0)))
                    continue
                attempts += 1
                seq += 1
                publish(
                    Snapshot(
                        terminal_id=terminal_id,
                        state=WorkerState.CONNECTING,
                        seq=seq,
                        connect_attempts=attempts,
                    )
                )
                ok, err = await account.connect()
                if not ok:
                    account.error = err
                    next_retry_at = time.monotonic() + _backoff_delay(backoff, attempts)
                    wlog.warning("connect attempt %d failed: %s", attempts, err)
                    seq += 1
                    publish(
                        Snapshot(
                            terminal_id=terminal_id,
                            state=WorkerState.FAILED,
                            seq=seq,
                            error=err,
                            connect_attempts=attempts,
                        )
                    )
                    continue
                wlog.info("connected after %d attempt(s)", attempts)
                attempts = 0

            seq += 1
            publish(await account.poll(terminal_id, seq, attempts))
            if not account.connected:
                next_retry_at = time.monotonic() + _backoff_delay(backoff, 1)
            await asyncio.sleep(interval)
    except Exception:
        wlog.exception("worker crashed")
        seq += 1
        publish(
            Snapshot(
                terminal_id=terminal_id,
                state=WorkerState.FAILED,
                seq=seq,
                error="worker crashed; see log",
            )
        )
    finally:
        await account.disconnect()
        seq += 1
        publish(Snapshot(terminal_id=terminal_id, state=WorkerState.STOPPED, seq=seq))
