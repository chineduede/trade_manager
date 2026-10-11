"""One cTrader account's connection, owned by one OS process: the cTrader twin of
:mod:`mt5copier.worker`.

It speaks the same messages to the supervisor -- a :class:`Snapshot` every tick, an
:class:`OrderResult` for every :class:`OrderCommand` -- so the engine, the reconciler and the UI
treat the account like any other slave. Underneath it is an asyncio loop around one
:class:`CTraderClient`, re-reading the account's positions and orders each tick.

Order commands go to a :class:`~mt5copier.ctrader.executor.CTraderExecutor`, on the same loop
and connection, between ticks.

A position's current price comes from a spot subscription, taken out for each symbol the account
holds a position in and kept until the connection ends. So do the prices that convert a cross's
money into the account's currency (:meth:`_Account.conversion`). What a snapshot leaves unknown is
``deals``: deposits and withdrawals come from a different request than trades, and a guard
rebuilding a balance from half the history would be wrong rather than blank.
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
from .client import DISCONNECT_EVENTS, CTraderClient, OpenApiError, host_for
from .executor import CTraderExecutor
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

SPOT_WAIT_S = 2.0
"""How long the first trade needing a currency conversion waits for its symbols' first prices.
cTrader sends a symbol's price right after the subscription, even with its market closed."""

_TOKEN_CODES = frozenset({"CH_ACCESS_TOKEN_INVALID", "OA_AUTH_TOKEN_EXPIRED", "ACCESS_DENIED"})


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
        self.asset_names: dict[int, str] = {}
        self.symbol_ids: dict[str, int] = {}
        self._assets: dict[int, tuple[int, int]] = {}
        """Each symbol's ``(base, quote)`` asset ids, from the symbol list."""
        self.symbols: dict[int, cv.SymbolInfo] = {}
        self._trader_read_at = 0.0
        self.error: str | None = None
        self.spots: dict[int, tuple[float | None, float | None]] = {}
        """``(bid, ask)`` by symbol id, from the spot subscriptions."""
        self._subscribed: set[int] = set()
        self._chains: dict[int, tuple[cv.Link, ...]] = {}
        """The conversion chain from each asset to the account's, as cTrader gave it."""

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
        # Subscriptions belong to a connection.
        self.spots = {}
        self._subscribed = set()
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
            self.asset_names = {int(a.assetId): a.name for a in assets.asset}
            self.currency = self.asset_names.get(int(self.trader.depositAssetId))
            listed = await client.request(
                oa.ProtoOASymbolsListReq(ctidTraderAccountId=self.account_id)
            )
            self.symbol_names = {int(s.symbolId): s.symbolName for s in listed.symbol}
            self.symbol_ids = {name: sid for sid, name in self.symbol_names.items()}
            self._assets = {
                int(s.symbolId): (int(s.baseAssetId), int(s.quoteAssetId)) for s in listed.symbol
            }
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

    @property
    def deposit_asset_id(self) -> int:
        return int(self.trader.depositAssetId) if self.trader is not None else 0

    def refresh_soon(self) -> None:
        """Re-read the balance on the next tick: something was just executed."""
        self._trader_read_at = 0.0

    async def _reconcile(self) -> tuple[list[Any], list[Any]]:
        """The open positions and the orders, as cTrader reports them now."""
        assert self.client is not None
        state = await self.client.request(
            oa.ProtoOAReconcileReq(ctidTraderAccountId=self.account_id)
        )
        return [p for p in state.position if cv.is_open(p)], list(state.order)

    async def used_margin(self) -> float | None:
        """The margin the open positions use now, asked of cTrader. None if it cannot say."""
        try:
            positions, _ = await self._reconcile()
        except (OpenApiError, OSError, TimeoutError, ConnectionError) as exc:
            log.warning("could not read the margin in use: %r", exc)
            return None
        return cv.money(sum(int(p.usedMargin) for p in positions), self.money_digits)

    async def open_positions(self) -> dict[int, Any]:
        """The account's open positions, by position id."""
        positions, _ = await self._reconcile()
        return {int(p.positionId): p for p in positions}

    async def resting_orders(self) -> dict[int, Any]:
        """The account's resting orders, by the ticket the copier knows them by
        (:func:`~mt5copier.ctrader.convert.order_ticket`)."""
        _, orders = await self._reconcile()
        return {cv.order_ticket(o): o for o in orders if cv.is_resting(o)}

    async def _subscribe(self, ids: set[int]) -> None:
        """Ask for spot prices of symbols not yet subscribed to. A refusal is logged and not
        retried on this connection: a price is wanted for display and breakeven, and must not
        stop the read."""
        missing = sorted(ids - self._subscribed)
        if not missing:
            return
        assert self.client is not None
        self._subscribed.update(missing)
        try:
            await self.client.request(
                oa.ProtoOASubscribeSpotsReq(ctidTraderAccountId=self.account_id, symbolId=missing)
            )
        except OpenApiError as exc:
            log.warning("cTrader refused spot prices for %s: %s", missing, exc)

    def _on_spot(self, event: Any) -> None:
        """A spot event carries only the side that changed."""
        sid = int(event.symbolId)
        bid, ask = self.spots.get(sid, (None, None))
        if event.HasField("bid"):
            bid = cv.spot_price(int(event.bid))
        if event.HasField("ask"):
            ask = cv.spot_price(int(event.ask))
        self.spots[sid] = (bid, ask)

    async def symbols_for(self, ids: set[int]) -> None:
        missing = sorted(i for i in ids if i not in self.symbols)
        if not missing:
            return
        assert self.client is not None
        answer = await self.client.request(
            oa.ProtoOASymbolByIdReq(ctidTraderAccountId=self.account_id, symbolId=missing)
        )
        for s in answer.symbol:
            sid = int(s.symbolId)
            base, quote = self._assets.get(sid, (0, 0))
            self.symbols[sid] = cv.SymbolInfo(
                symbol_id=sid,
                name=self._symbol_name(sid),
                digits=int(s.digits),
                lot_size=int(s.lotSize),
                min_volume=int(s.minVolume),
                max_volume=int(s.maxVolume),
                step_volume=int(s.stepVolume),
                base_asset_id=base,
                quote_asset_id=quote,
            )

    async def conversion(self, asset_id: int) -> float | str:
        """What one unit of ``asset_id`` is worth in the account's currency, valued high (see
        :func:`~mt5copier.ctrader.convert.conversion_rate`), or why it is not known.

        The chain of symbols linking the two is asked of cTrader once per asset, and their
        prices subscribed to. Their first prices are waited for, up to :data:`SPOT_WAIT_S`;
        without them the answer is "no price yet", never a guess. A refusal is an answer
        here, not an error, so that a caller who can do without the rate carries on."""
        deposit = self.deposit_asset_id
        if asset_id == deposit:
            return 1.0
        assert self.client is not None
        chain = self._chains.get(asset_id)
        if chain is None:
            try:
                answer = await self.client.request(
                    oa.ProtoOASymbolsForConversionReq(
                        ctidTraderAccountId=self.account_id,
                        firstAssetId=asset_id,
                        lastAssetId=deposit,
                    )
                )
            except OpenApiError as exc:
                return f"cTrader cannot convert {self._asset(asset_id)} to {self._asset(deposit)}: {exc}"
            chain = tuple(self._link(s) for s in answer.symbol)
            self._chains[asset_id] = chain
            log.info(
                "converting %s to %s through %s",
                self._asset(asset_id),
                self._asset(deposit),
                ", ".join(self._symbol_name(x.symbol_id) for x in chain) or "nothing",
            )
        if not chain:
            return (
                f"cTrader knows no way to convert {self._asset(asset_id)} to {self._asset(deposit)}"
            )
        ids = {x.symbol_id for x in chain}
        await self._subscribe(ids)
        await self._await_spots(ids, SPOT_WAIT_S)
        rate = cv.conversion_rate(chain, asset_id, deposit, self.spots)
        if rate is not None:
            return rate
        unpriced = sorted(self._symbol_name(i) for i in ids if not self._priced(i))
        if unpriced:
            return f"no price for {', '.join(unpriced)} yet"
        return (
            f"cTrader's symbols do not lead from {self._asset(asset_id)} to {self._asset(deposit)}"
        )

    def _link(self, light: Any) -> cv.Link:
        sid = int(light.symbolId)
        if light.HasField("baseAssetId") and light.HasField("quoteAssetId"):
            return cv.Link(sid, int(light.baseAssetId), int(light.quoteAssetId))
        base, quote = self._assets.get(sid, (0, 0))
        return cv.Link(sid, base, quote)

    def _asset(self, asset_id: int) -> str:
        return self.asset_names.get(asset_id, f"asset #{asset_id}")

    def _symbol_name(self, symbol_id: int) -> str:
        return self.symbol_names.get(symbol_id, f"#{symbol_id}")

    def _priced(self, symbol_id: int) -> bool:
        bid, ask = self.spots.get(symbol_id, (None, None))
        return bool(bid) and bool(ask)

    async def _await_spots(self, ids: set[int], timeout: float) -> bool:
        """Wait until every symbol in ``ids`` has a bid and an ask. Spot events are read as they
        come; every other event is put back, in order, for the next tick to read."""
        assert self.client is not None
        client = self.client
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        kept: list[Any] = []
        try:
            while not all(self._priced(i) for i in ids):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    event = await asyncio.wait_for(client.events.get(), remaining)
                except TimeoutError:
                    return False
                if isinstance(event, oa.ProtoOASpotEvent):
                    self._on_spot(event)
                    continue
                kept.append(event)
                if isinstance(event, DISCONNECT_EVENTS):
                    return False
            return True
        finally:
            for event in kept:
                client.events.put_nowait(event)

    def _drain_events(self) -> bool:
        """Read what the server sent unasked. True if an execution happened."""
        assert self.client is not None
        executed = False
        while True:
            try:
                event = self.client.events.get_nowait()
            except asyncio.QueueEmpty:
                return executed
            if isinstance(event, DISCONNECT_EVENTS):
                reason = getattr(event, "reason", "") or type(event).__name__
                raise ConnectionError(f"cTrader ended the session: {reason}")
            if isinstance(event, oa.ProtoOAExecutionEvent):
                executed = True
            elif isinstance(event, oa.ProtoOASpotEvent):
                self._on_spot(event)

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
        raw_positions, raw_orders = await self._reconcile()
        await self.symbols_for(
            {int(p.tradeData.symbolId) for p in raw_positions}
            | {int(o.tradeData.symbolId) for o in raw_orders}
        )
        await self._subscribe({int(p.tradeData.symbolId) for p in raw_positions})
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
            cv.position_live(
                p,
                pnl.get(int(p.positionId)),
                self.money_digits,
                self.spots.get(int(p.tradeData.symbolId)),
            )
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
            cv.money(used, self.money_digits),
        )

    @property
    def money_digits(self) -> int:
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
        balance = cv.money(int(trader.balance), self.money_digits) if trader else None
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
    executor = CTraderExecutor(terminal_id, account)
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
                    result = await executor.execute(cmd)
                    wlog.info("%s", result.describe())
                    publish_result(result)
                    continue
                if isinstance(cmd, Command) and cmd.kind is CommandKind.RECONNECT:
                    wlog.info("reconnect requested")
                    await account.disconnect()
                    executor.forget_symbols()
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
