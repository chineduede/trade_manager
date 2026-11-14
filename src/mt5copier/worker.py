"""One MT5 terminal connection, owned by one OS process.

The ``MetaTrader5`` package keeps its connection in process-global state: ``initialize()`` and
every subsequent call operate on a single implicit session per process. There is no handle to
pass around and no way to hold two sessions at once, so N terminals require N processes. This
module is the body of one such process.

``MetaTrader5`` is imported lazily inside :func:`run_worker` rather than at module scope, so
that the supervisor -- which imports this module to get at ``run_worker`` -- never loads the
extension into the parent process.

Two behaviours here are direct responses to gaps in ``zele-mt5``:

* **Reconnection.** That project connected once in a constructor and had no retry, no backoff
  and no health check, so a dropped broker connection killed the worker permanently. Here the
  connection is a loop with a backoff schedule and continuous liveness checks.
* **Reads never tear down the session.** Its ``get_acct_info()`` called ``mt5.shutdown()`` when
  a read failed, destroying the process's connection as a side effect of a query. Here a failed
  read only marks state; teardown happens exclusively in the reconnect path.
"""

from __future__ import annotations

import os
import queue
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from .discovery import find_terminal_pid
from .execution import OrderTime
from .executor import UNREADABLE, Executor, LivePosition, ReadFailure
from .logging_setup import configure, get_logger
from .protocol import (
    Candle,
    Command,
    CommandKind,
    Deal,
    OrderCommand,
    OrderResult,
    OrderType,
    PendingOrder,
    Position,
    PositionLive,
    PositionType,
    Quote,
    RatesRequest,
    RatesResult,
    Snapshot,
    WatchCommand,
    WorkerState,
)
from .sizing import SymbolFacts
from .symbols import resolve as resolve_symbol

if TYPE_CHECKING:
    from multiprocessing.queues import Queue

log = get_logger(__name__)

# Guard against a hung terminal blocking the worker forever. MT5's own default is 60s.
_INIT_TIMEOUT_MS = 20_000


class _Session:
    """Thin wrapper over the process-global MT5 module.

    Exists to keep the polling loop readable and to give tests a seam: the loop only ever talks
    to this object, so a fake session can be injected without patching module globals.
    """

    def __init__(self, mt5: Any, exe_path: Path) -> None:
        self._mt5 = mt5
        self._exe_path = exe_path
        self.connected = False
        self._launched_pid: int | None = None
        """The terminal's pid, set only when *this* ``connect()`` call was the one that started
        it -- i.e. nothing was running at ``exe_path`` the instant before. None means either we
        are not connected, or the terminal was already running, which makes it the user's to
        close, not ours. Re-derived on every connect, so a terminal we launched that later
        crashes and gets relaunched by hand is correctly no longer considered ours."""
        self._clock = _ServerClock()
        self._deals: tuple[Deal, ...] | None = None
        self._deals_since: float | None = None
        self._deals_read_at = 0.0
        self._deals_balance: float | None = None
        self.watch: WatchCommand | None = None
        """The symbols to quote on every snapshot, as the supervisor last asked. Latest wins;
        set from the command loop, read by :meth:`poll`."""
        self._watch_resolved: WatchCommand | None = None
        """Which watch command :attr:`_quote_names` was resolved for -- identity, not value,
        so a re-sent identical command costs nothing and a new one re-resolves."""
        self._quote_names: dict[str, str | None] = {}
        """Clean name -> this broker's name, or None for one that cannot be resolved (said
        once, at resolution, rather than every poll)."""

    def connect(self) -> tuple[bool, str | None]:
        """Call ``initialize(path=...)``. Returns (ok, error message).

        This is the only place that may launch a terminal: per the MT5 docs, ``initialize()``
        starts the terminal if it is not already running. That is why discovery never calls it
        and why this runs only for terminals the user has explicitly designated.

        Notes whether this call is the one that launched it, by checking just before: an
        instance already running is the user's own, not ours to ever close later.
        """
        already_running = find_terminal_pid(self._exe_path) is not None
        try:
            ok = self._mt5.initialize(str(self._exe_path), timeout=_INIT_TIMEOUT_MS)
        except Exception as exc:  # the extension can raise, not just return False
            return False, f"initialize() raised: {exc!r}"
        if not ok:
            return False, f"initialize() failed: {self._last_error()}"
        self.connected = True
        # The account may not be the one this session last read, so its history is not kept.
        self._deals = self._deals_since = self._deals_balance = None
        self._deals_read_at = 0.0
        # The broker's symbol list may differ across a reconnect, so watched names are
        # resolved against it afresh.
        self._watch_resolved = None
        self._quote_names = {}
        self._launched_pid = None if already_running else find_terminal_pid(self._exe_path)
        return True, None

    def disconnect(self, *, close_terminal: bool = False, close_any: bool = False) -> None:
        """Close the MT5 session. With ``close_terminal=True``, also close the terminal
        process itself -- but only the one this session launched, unless ``close_any`` says to
        close whichever is running from this exe, the person's own included.

        Plain disconnect (the reconnect path) must never close the terminal: it would kill the
        very instance a moment before reconnecting to it. Only a caller that is done with this
        terminal for good passes ``close_terminal=True``.
        """
        if self.connected:
            try:
                self._mt5.shutdown()
            except Exception as exc:
                log.debug("shutdown() raised, ignoring: %r", exc)
            finally:
                self.connected = False
        if close_terminal:
            self._close_terminal(close_any=close_any)

    def _close_terminal(self, *, close_any: bool) -> None:
        pid = self._launched_pid
        self._launched_pid = None
        ours = pid is not None
        if pid is None and close_any:
            pid = find_terminal_pid(self._exe_path)
        if pid is None:
            return
        try:
            proc = psutil.Process(pid)
            if str(proc.exe()).lower() != str(self._exe_path).lower():
                # The pid was recycled for something else entirely; refuse to touch it.
                log.warning("pid %s is no longer the terminal; not closing", pid)
                return
            proc.terminate()
            log.info(
                "closed terminal pid=%s (%s)",
                pid,
                "we launched it" if ours else "it was open already",
            )
        except psutil.NoSuchProcess:
            pass  # already gone
        except Exception as exc:
            log.debug("could not close terminal pid=%s: %r", pid, exc)

    def _last_error(self) -> str:
        try:
            return str(self._mt5.last_error())
        except Exception:
            return "(last_error unavailable)"

    def _empty_or_unknown(self, what: str, total_attr: str) -> tuple[()] | None:
        """Disambiguate the ``None`` that ``positions_get()`` / ``orders_get()`` return.

        MT5 uses ``None`` for two entirely different answers -- "the call failed" and, on some
        builds, "there is nothing to return" -- and the two must not be conflated: an empty
        list is acted on (close the copies, cancel the copies) while an unknown one is skipped.
        Guessing wrong in one direction cancels live orders; in the other it silently freezes
        the copier.

        Rather than interpret ``last_error()``, ask the matching count function. A count of
        zero settles it; anything else, including a count that will not read, stays unknown.
        """
        try:
            # Resolved inside the try: an extension that lacks the call at all must read as
            # unknown, not raise out of the poll and take the worker down with it.
            total = getattr(self._mt5, total_attr)()
        except Exception as exc:
            log.debug("%s count unavailable after a None read: %r", what, exc)
            return None
        if total == 0:
            return ()
        log.debug("%s read returned None while %r are open; treating as unknown", what, total)
        return None

    # --- what the executor needs ----------------------------------------------------------
    #
    # Thin, forgiving wrappers: each returns None rather than raising, because a missing or
    # misbehaving read must become a refusal to trade, not an exception that kills the worker.

    def server_offset_s(self) -> int | None:
        """How far this broker's clock runs ahead of UTC, or None while still unknown.

        The executor turns a command's UTC expiry into this broker's own clock with it --
        the same offset the polling loop stamps on every snapshot."""
        return self._clock.offset

    def account_trade_mode(self) -> int | None:
        """``account_info().trade_mode``. None when it cannot be read, which the executor
        treats as "not a demo"."""
        try:
            account = self._mt5.account_info()
        except Exception as exc:
            log.debug("account_info() raised while checking trade mode: %r", exc)
            return None
        mode = getattr(account, "trade_mode", None) if account is not None else None
        try:
            return None if mode is None else int(mode)
        except (TypeError, ValueError):
            return None

    def symbol_names(self) -> list[str]:
        try:
            symbols = self._mt5.symbols_get()
        except Exception as exc:
            log.debug("symbols_get() raised: %r", exc)
            return []
        if not symbols:
            return []
        return [str(s.name) for s in symbols if getattr(s, "name", None)]

    def symbol_facts(self, name: str) -> SymbolFacts | None:
        """``symbol_info``, selecting the symbol into Market Watch if it isn't already visible.

        ``.visible`` rather than "was ``symbol_info`` ``None``" is the right check -- an
        unselected symbol does not reliably answer with ``None``, some brokers hand back a
        non-None struct with zeroed fields instead, a filling mask of zero being the most
        dangerous one. Re-reading after a *successful* select is for the same reason: the
        struct read before selecting can still be the stale, zeroed one.

        If the select call itself reports failure, one retry after a short wait follows rather
        than failing outright -- a terminal that just can't act on it instantly is not the same
        as a symbol that does not exist.
        """
        try:
            info = self._mt5.symbol_info(name)
            if info is None:
                return None
            if not info.visible:
                if self._mt5.symbol_select(name, True):
                    info = self._mt5.symbol_info(name)
                else:
                    log.warning("symbol_select(%r) failed; retrying after a short wait", name)
                    time.sleep(2.0)
                    info = self._mt5.symbol_info(name)
        except Exception as exc:
            log.debug("symbol_info(%r) raised: %r", name, exc)
            return None
        if info is None:
            return None
        try:
            return SymbolFacts(
                name=str(getattr(info, "name", name)),
                volume_min=float(info.volume_min),
                volume_max=float(info.volume_max),
                volume_step=float(info.volume_step),
                point=float(getattr(info, "point", 0.0) or 0.0),
                digits=int(getattr(info, "digits", 5) or 5),
                filling_mask=int(getattr(info, "filling_mode", 0) or 0),
                expiration_mask=int(getattr(info, "expiration_mode", 0) or 0),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            log.warning("unusable symbol_info for %r: %s", name, exc)
            return None

    def tick(self, name: str) -> tuple[float, float] | None:
        """Current ``(bid, ask)``. Re-read on every reprice; never cached."""
        try:
            quote = self._mt5.symbol_info_tick(name)
        except Exception as exc:
            log.debug("symbol_info_tick(%r) raised: %r", name, exc)
            return None
        if quote is None:
            return None
        try:
            return float(quote.bid), float(quote.ask)
        except (AttributeError, TypeError, ValueError):
            return None

    def loss_per_lot(
        self, direction: PositionType, symbol: str, entry: float, sl: float
    ) -> float | None:
        """What one lot loses between two prices, in the account currency.

        Asks the terminal via ``order_calc_profit`` rather than deriving it from tick values:
        it is exact, and it handles cross-currency instruments without us converting anything.
        """
        try:
            profit = self._mt5.order_calc_profit(int(direction), symbol, 1.0, entry, sl)
        except Exception as exc:
            log.debug("order_calc_profit(%r) raised: %r", symbol, exc)
            return None
        if profit is None:
            return None
        try:
            return abs(float(profit))
        except (TypeError, ValueError):
            return None

    def deal_commission(self, deal: int) -> float | None:
        """The ``commission`` field of one deal, as MT5 records it: negative for a charge. None
        when the deal cannot be read. A just-made deal can take a moment to reach the history,
        so a miss is tried again, briefly."""
        for attempt in range(3):
            try:
                found = self._mt5.history_deals_get(ticket=deal)
            except Exception as exc:
                log.debug("history_deals_get(ticket=%s) raised: %r", deal, exc)
                return None
            if found:
                try:
                    return float(getattr(found[0], "commission", 0.0) or 0.0)
                except (TypeError, ValueError):
                    return None
            if attempt < 2:
                time.sleep(0.1)
        return None

    def position_commission(self, ticket: int) -> tuple[float, float] | None:
        """``(commission, volume)`` of the deal that opened position ``ticket``, the
        commission as MT5 records it: negative for a charge. None when it cannot be read."""
        try:
            found = self._mt5.history_deals_get(position=ticket)
        except Exception as exc:
            log.debug("history_deals_get(position=%s) raised: %r", ticket, exc)
            return None
        # DEAL_ENTRY_IN: the deal that opened it, rather than a later partial close.
        opening = next((d for d in found or () if int(getattr(d, "entry", -1)) == 0), None)
        if opening is None:
            return None
        try:
            return float(getattr(opening, "commission", 0.0) or 0.0), float(opening.volume)
        except (AttributeError, TypeError, ValueError):
            return None

    def margin_for(
        self, direction: PositionType, symbol: str, volume: float, price: float
    ) -> float | None:
        """The margin ``volume`` lots would take, in the account currency, via
        ``order_calc_margin``. None if the terminal cannot say."""
        try:
            margin = self._mt5.order_calc_margin(int(direction), symbol, volume, price)
        except Exception as exc:
            log.debug("order_calc_margin(%r) raised: %r", symbol, exc)
            return None
        try:
            return None if margin is None else float(margin)
        except (TypeError, ValueError):
            return None

    def margin_used(self) -> float | None:
        """``account_info().margin``: what the open positions use now. None if unreadable."""
        try:
            account = self._mt5.account_info()
            margin = getattr(account, "margin", None) if account is not None else None
            return None if margin is None else float(margin)
        except Exception as exc:
            log.debug("account_info() raised while reading the margin in use: %r", exc)
            return None

    def position(self, ticket: int) -> LivePosition | None | ReadFailure:
        """``(symbol, direction, volume)`` for one open position, read live.

        Closing, reducing and modifying use the terminal's own figures rather than whatever we
        last recorded. Our record can be stale -- the user may have closed part of it by hand
        -- and a stale volume produces an INVALID_VOLUME rejection at best, or closes the wrong
        amount at worst.

        **Three answers, not two.** ``None`` means the terminal answered and the position is
        not there; ``UNREADABLE`` means it would not answer. The executor reports "not there"
        as a success, so collapsing the two would turn a momentary read failure into a close
        that never happened or a stop that never moved -- recorded as done either way. An
        empty sequence is MT5's unambiguous "nothing matched"; a bare ``None`` is its
        overloaded one, and is read here as unknown.
        """
        try:
            found = self._mt5.positions_get(ticket=ticket)
        except Exception as exc:
            log.debug("positions_get(ticket=%r) raised: %r", ticket, exc)
            return UNREADABLE
        if found is None:
            return UNREADABLE
        if len(found) == 0:
            return None
        p = found[0]
        try:
            return str(p.symbol), PositionType(int(p.type)), float(p.volume)
        except (AttributeError, TypeError, ValueError) as exc:
            # Unparseable is not absent, for the same reason it is not in _read_positions.
            log.warning("unusable position %r for ticket %s: %s", p, ticket, exc)
            return UNREADABLE

    def order_exists(self, ticket: int) -> bool:
        """Whether a pending order is still resting.

        A read failure answers **True**, which sends a cancel or a modify that may be
        redundant -- both are no-ops against an order that has gone. The alternative is far
        worse: answering False reports "already gone", and every caller treats that as the
        desired state already being true, so a live resting order would be dropped from
        belief and left untracked.

        That means a bare ``None`` has to answer True as well, not just an exception. MT5
        overloads ``None`` for "the call failed" and "nothing matched" -- the same trap
        :meth:`_empty_or_unknown` exists for -- and ``bool(None)`` quietly picks the wrong
        one. Only an empty sequence is an unambiguous "not there".
        """
        try:
            found = self._mt5.orders_get(ticket=ticket)
        except Exception as exc:
            log.debug("orders_get(ticket=%r) raised: %r", ticket, exc)
            return True
        if found is None:
            log.debug("orders_get(ticket=%r) gave None; assuming it is still resting", ticket)
            return True
        return len(found) > 0

    def order_send(self, request: dict[str, Any]) -> Any:
        """The one call in this project that creates an order. Returns the raw MT5 result, or
        None if the call itself failed -- which the executor classifies as a connection
        problem rather than a rejection."""
        try:
            return self._mt5.order_send(request)
        except Exception as exc:
            log.warning("order_send raised: %r", exc)
            return None

    def _read_quotes(self, now: float) -> tuple[Quote, ...]:
        """Current prices for the watched symbols, answered in their clean names.

        A symbol that cannot be resolved or will not tick is simply absent -- the virtual
        master treats a missing quote as unknown and does nothing on it, which is the safe
        side of every guess available here.
        """
        watch = self.watch
        if watch is None or not watch.symbols:
            return ()
        if self._watch_resolved is not watch:
            self._resolve_watch(watch)
        out: list[Quote] = []
        for clean in watch.symbols:
            name = self._quote_names.get(clean)
            if name is None:
                continue
            quote = self.tick(name)
            if quote is None:
                continue
            out.append(Quote(symbol=clean, bid=quote[0], ask=quote[1], ts=now))
        return tuple(out)

    def _resolve_watch(self, watch: WatchCommand) -> None:
        """Turn each watched clean name into this broker's own, once per watch command.

        The same resolution the executor applies to a trade's symbol, against the same list,
        so the price feeding the book is quoted on exactly the instrument a copy would be
        placed on. ``symbol_facts`` is read once per hit to select the symbol into Market
        Watch -- an unselected symbol does not reliably tick.
        """
        available = self.symbol_names()
        if not available:
            return  # the list would not read; tried again next poll
        resolved: dict[str, str | None] = {}
        symbol_map = dict(watch.symbol_map)
        for clean in watch.symbols:
            answer = resolve_symbol(
                clean,
                available,
                watch.symbol_prefix,
                watch.symbol_suffix,
                "the feed terminal",
                symbol_map,
            )
            if answer.name is None:
                log.warning("cannot quote %r for the virtual master: %s", clean, answer.how)
                resolved[clean] = None
                continue
            self.symbol_facts(answer.name)
            resolved[clean] = answer.name
        self._quote_names = resolved
        self._watch_resolved = watch

    def read_rates(self, req: RatesRequest) -> tuple[Candle, ...] | str:
        """The last ``req.count`` bars of one clean symbol, oldest first, or why they will
        not read. Resolution is the watch's own (:meth:`_resolve_watch`), so the chart shows
        exactly the instrument the quotes feeding the book are read from."""
        available = self.symbol_names()
        if not available:
            return "the broker's symbol list would not read"
        answer = resolve_symbol(
            req.symbol,
            available,
            req.symbol_prefix,
            req.symbol_suffix,
            "the feed terminal",
            dict(req.symbol_map),
        )
        if answer.name is None:
            return answer.how
        self.symbol_facts(answer.name)
        try:
            rows = self._mt5.copy_rates_from_pos(answer.name, req.timeframe, 0, req.count)
        except Exception as exc:
            return f"the bars would not read: {exc!r}"
        if rows is None:
            return "the terminal answered no bars"
        return tuple(
            Candle(
                ts=int(row["time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
            for row in rows
        )

    def poll(self, terminal_id: str, seq: int, attempts: int) -> Snapshot:
        """Read account and terminal state into a snapshot.

        A read failure marks the session as not connected so the caller reconnects on the next
        pass, but never calls ``shutdown()`` here -- teardown is the reconnect path's job.
        """
        try:
            account = self._mt5.account_info()
            terminal = self._mt5.terminal_info()
        except Exception as exc:
            self.connected = False
            return Snapshot(
                terminal_id=terminal_id,
                state=WorkerState.DISCONNECTED,
                seq=seq,
                error=f"read failed: {exc!r}",
                connect_attempts=attempts,
            )

        if account is None or terminal is None:
            self.connected = False
            return Snapshot(
                terminal_id=terminal_id,
                state=WorkerState.DISCONNECTED,
                seq=seq,
                error=f"account/terminal info unavailable: {self._last_error()}",
                connect_attempts=attempts,
            )

        open_positions, live_positions = self._read_positions()
        # The clock first: an order's expiry is reported in the broker's own time, so reading
        # the orders without the offset would leave every expiry uncopyable this tick.
        offset = self._clock.update(self._mt5, open_positions, now=time.time())
        pending_orders = self._read_orders(offset)
        balance = getattr(account, "balance", None)
        self._refresh_deals(offset, balance, now=time.time())

        # A terminal can be reachable while its broker link is down; report that distinctly
        # rather than as a hard failure, because the process is healthy and will recover.
        broker_up = bool(getattr(terminal, "connected", True))

        return Snapshot(
            terminal_id=terminal_id,
            state=WorkerState.CONNECTED if broker_up else WorkerState.DISCONNECTED,
            seq=seq,
            login=getattr(account, "login", None),
            server=getattr(account, "server", None),
            company=getattr(account, "company", None),
            currency=getattr(account, "currency", None),
            balance=getattr(account, "balance", None),
            equity=getattr(account, "equity", None),
            margin_free=getattr(account, "margin_free", None),
            leverage=getattr(account, "leverage", None),
            margin=_opt_float(account, "margin"),
            margin_level=_opt_float(account, "margin_level"),
            profit=_opt_float(account, "profit"),
            trade_allowed=getattr(terminal, "trade_allowed", None),
            trade_mode=getattr(account, "trade_mode", None),
            terminal_connected=broker_up,
            terminal_build=getattr(terminal, "build", None),
            ping_last_us=_opt_int(terminal, "ping_last"),
            positions=None if open_positions is None else len(open_positions),
            open_positions=open_positions,
            live_positions=live_positions,
            pending_orders=pending_orders,
            server_offset_s=offset,
            deals=self._deals,
            deals_since=self._deals_since,
            quotes=self._read_quotes(time.time()),
            error=None if broker_up else "terminal not connected to broker",
            connect_attempts=attempts,
        )

    def _refresh_deals(self, offset: int | None, balance: float | None, *, now: float) -> None:
        """Re-read the recent deal history when it may have changed.

        Not on every poll: the history is the one read here that grows with the account's
        activity. Every few seconds, and at once whenever the balance moves -- a close, a
        deposit -- so a guard never works from a history that is missing the deal it just saw
        land in the balance. Without the server's offset there is no knowing which deals fall
        in the window, so nothing is read and the last answer stands as unknown.
        """
        if offset is None:
            return
        due = now - self._deals_read_at >= _DEALS_REFRESH_S or balance != self._deals_balance
        if not due:
            return
        since = now - _DEALS_WINDOW_S
        deals = self._read_deals(since, offset, now=now)
        self._deals_read_at = now
        if deals is None:
            self._deals = self._deals_since = None
            return
        self._deals, self._deals_since, self._deals_balance = deals, since, balance

    def _read_deals(self, since: float, offset: int, *, now: float) -> tuple[Deal, ...] | None:
        """Deals from ``since`` (real UTC) to now, oldest first, or None if they could not be
        read. Asked for in the broker's clock, which is how MT5 takes and gives every time."""
        try:
            raw = self._mt5.history_deals_get(int(since) + offset, int(now) + offset + 3600)
        except Exception as exc:
            log.debug("history_deals_get() raised: %r", exc)
            return None
        if raw is None:
            # As with positions: None is both "no deals" and "failed". A count of zero over the
            # same window is what tells them apart.
            try:
                total = self._mt5.history_deals_total(int(since) + offset, int(now) + offset + 3600)
            except Exception as exc:
                log.debug("history_deals_total() raised: %r", exc)
                return None
            return () if total == 0 else None
        out: list[Deal] = []
        for d in raw:
            try:
                msc = int(getattr(d, "time_msc", 0) or 0)
                server_time = msc / 1000 if msc else float(d.time)
                out.append(
                    Deal(
                        ticket=int(d.ticket),
                        time=server_time - offset,
                        type=int(d.type),
                        entry=int(getattr(d, "entry", 0) or 0),
                        profit=float(getattr(d, "profit", 0.0) or 0.0),
                        commission=float(getattr(d, "commission", 0.0) or 0.0),
                        swap=float(getattr(d, "swap", 0.0) or 0.0),
                        fee=float(getattr(d, "fee", 0.0) or 0.0),
                        symbol=str(getattr(d, "symbol", "") or ""),
                        position=int(getattr(d, "position_id", 0) or 0),
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                # One deal we cannot read would leave a hole in a rebuilt balance, so the whole
                # history counts as unknown rather than quietly short.
                log.warning("unparseable deal %r (%s); treating history as unknown", d, exc)
                return None
        out.sort(key=lambda deal: deal.time)
        return tuple(out)

    def _read_orders(self, offset: int | None = None) -> tuple[PendingOrder, ...] | None:
        """Read the resting orders, or None if they could not be read.

        Same discipline as :meth:`_read_positions`, and for a sharper reason: an order missing
        from this list is read as a cancellation, so a failed read misreported as an empty list
        would propose cancelling every copied order at once.

        ``offset`` is the broker's clock offset, for turning an order's expiry into real UTC.
        While it is still unknown the expiry is reported as none -- the copy rests GTC for a
        moment -- and the tick that finds the offset reports the real expiry, which the diff
        then applies as an ordinary order modification.
        """
        try:
            raw = self._mt5.orders_get()
        except Exception as exc:
            log.debug("orders_get() raised: %r", exc)
            return None
        if raw is None:
            return self._empty_or_unknown("orders", "orders_total")

        out: list[PendingOrder] = []
        for o in raw:
            try:
                out.append(
                    PendingOrder(
                        ticket=int(o.ticket),
                        symbol=str(o.symbol),
                        type=OrderType(int(o.type)),
                        volume=float(getattr(o, "volume_current", 0.0) or 0.0),
                        price_open=float(o.price_open),
                        sl=float(getattr(o, "sl", 0.0) or 0.0),
                        tp=float(getattr(o, "tp", 0.0) or 0.0),
                        price_stoplimit=float(getattr(o, "price_stoplimit", 0.0) or 0.0),
                        time_setup=int(getattr(o, "time_setup", 0) or 0),
                        magic=int(getattr(o, "magic", 0) or 0),
                        comment=str(getattr(o, "comment", "") or ""),
                        expiration_utc=_expiration_utc(o, offset),
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                log.warning("unparseable order %r (%s); treating read as failed", o, exc)
                return None
        return tuple(out)

    def _read_positions(
        self,
    ) -> tuple[tuple[Position, ...] | None, tuple[PositionLive, ...] | None]:
        """Read the open positions and their live values, or ``(None, None)`` if they could
        not be read. One ``positions_get()`` call fills both: the live half was always in the
        reply and used to be thrown away.

        Replaces the ``positions_total()`` count rather than adding to it: the list already
        carries the count, and one IPC round-trip per tick is cheaper than two.

        Returning None on failure -- and never an empty tuple -- is deliberate. The reconciler
        reads an empty list as a flat master and would propose closing every copy, so a read
        error must be indistinguishable from "do not reconcile", not from "nothing open".
        """
        try:
            raw = self._mt5.positions_get()
        except Exception as exc:
            log.debug("positions_get() raised: %r", exc)
            return None, None
        if raw is None:
            empty = self._empty_or_unknown("positions", "positions_total")
            return empty, empty

        out: list[Position] = []
        live: list[PositionLive] = []
        for p in raw:
            try:
                out.append(
                    Position(
                        ticket=int(p.ticket),
                        symbol=str(p.symbol),
                        type=PositionType(int(p.type)),
                        volume=float(p.volume),
                        price_open=float(p.price_open),
                        sl=float(getattr(p, "sl", 0.0) or 0.0),
                        tp=float(getattr(p, "tp", 0.0) or 0.0),
                        time=int(getattr(p, "time", 0) or 0),
                        magic=int(getattr(p, "magic", 0) or 0),
                        comment=str(getattr(p, "comment", "") or ""),
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                # A position we cannot parse must not silently vanish from the list, because
                # absence from the master's list is what a CLOSE is derived from. Fail the whole
                # read instead, so this tick is skipped rather than misread.
                log.warning("unparseable position %r (%s); treating read as failed", p, exc)
                return None, None
            live.append(
                PositionLive(
                    ticket=out[-1].ticket,
                    price_current=_opt_float(p, "price_current"),
                    profit=_opt_float(p, "profit"),
                    swap=_opt_float(p, "swap"),
                )
            )
        return tuple(out), tuple(live)


_DEALS_REFRESH_S = 5.0
_DEALS_WINDOW_S = 50 * 3600.0
"""How far back the deal history goes. A daily reset is at most a day ago; the rest is room for
time zones and for a reset that falls just after a weekend's quiet."""


class _ServerClock:
    """Works out how far the broker's clock is from UTC.

    MT5 has no call for the server's time. What it has is tick times, stamped in the broker's
    clock as if that were UTC. A tick that has *just changed* was stamped a moment ago, so its
    time less ours is the offset, give or take latency -- rounded to the half hour every real
    offset is a multiple of. A tick that has not changed since the last look says nothing: on a
    closed market it may be days old, and rounding that would give a confident wrong answer.
    """

    _RECHECK_S = 60.0
    """Once known, how often to look again -- an offset only moves with daylight saving."""
    _PROBE_S = 1.0
    _STEP_S = 1800
    _MAX_S = 14 * 3600
    _PROBES = 3

    def __init__(self) -> None:
        self.offset: int | None = None
        self._checked_at = 0.0
        self._seen: dict[str, float] = {}
        self._watch: list[str] | None = None

    def update(self, mt5: Any, positions: tuple[Position, ...] | None, *, now: float) -> int | None:
        wait = self._RECHECK_S if self.offset is not None else self._PROBE_S
        if now - self._checked_at < wait:
            return self.offset
        self._checked_at = now
        names = list(dict.fromkeys([p.symbol for p in positions or ()] + self._watched(mt5)))
        for name in names:
            stamp = _tick_time(mt5, name)
            if stamp is None:
                continue
            before = self._seen.get(name)
            self._seen[name] = stamp
            if before is None or stamp <= before:
                continue
            ahead = stamp - now
            offset = round(ahead / self._STEP_S) * self._STEP_S
            if abs(offset) <= self._MAX_S and abs(ahead - offset) < 300:
                if offset != self.offset:
                    log.info("broker clock is UTC%+.1fh", offset / 3600)
                self.offset = offset
                break
        return self.offset

    def _watched(self, mt5: Any) -> list[str]:
        """A few Market Watch symbols, so the offset can be found with nothing open. Listed
        once per session: the full symbol list can run to thousands."""
        if self._watch is None:
            try:
                symbols = mt5.symbols_get() or ()
                self._watch = [str(s.name) for s in symbols if getattr(s, "visible", False)][
                    : self._PROBES
                ]
            except Exception as exc:
                log.debug("symbols_get() raised: %r", exc)
                self._watch = []
        return self._watch


def _expiration_utc(order: Any, offset: int | None) -> int:
    """An order's broker-side expiry in real UTC seconds, or 0 when there is none to copy.

    Only ``ORDER_TIME_SPECIFIED`` carries a timestamp worth mirroring: GTC has none, and a
    DAY order's "day" is the *broker's* trading day, which another broker's clock ends at a
    different moment -- copying it as a timestamp would be inventing one. The timestamp is in
    the broker's own clock, so without the clock offset it cannot be placed on the UTC line
    yet and reads as none; the tick that finds the offset reports it properly.
    """
    if offset is None:
        return 0
    try:
        if int(getattr(order, "type_time", 0) or 0) != int(OrderTime.SPECIFIED):
            return 0
        expiration = int(getattr(order, "time_expiration", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return expiration - offset if expiration > 0 else 0


def _tick_time(mt5: Any, name: str) -> float | None:
    """The last tick's time for ``name`` in the broker's clock, to the millisecond if given."""
    try:
        tick = mt5.symbol_info_tick(name)
    except Exception:
        return None
    if tick is None:
        return None
    msc = _opt_int(tick, "time_msc")
    if msc:
        return msc / 1000
    seconds = _opt_int(tick, "time")
    return float(seconds) if seconds else None


def _opt_float(obj: object, name: str) -> float | None:
    """A display-only field, or None if it is missing or not a number. Never raises: these
    values decorate a snapshot and must not be able to fail one."""
    value = getattr(obj, name, None)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(obj: object, name: str) -> int | None:
    """As :func:`_opt_float`, for an integer field."""
    value = getattr(obj, name, None)
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _backoff_delay(schedule: list[float], attempt: int) -> float:
    """Delay before retry ``attempt`` (1-based); the final entry repeats indefinitely."""
    if not schedule:
        return 5.0
    return schedule[min(attempt, len(schedule)) - 1]


def run_worker(
    terminal_id: str,
    exe_path: str,
    out_q: Queue,
    cmd_q: Queue,
    log_file: str,
    poll_interval_ms: int = 250,
    reconnect_backoff_s: list[float] | None = None,
    _session_factory: Callable[[Path], _Session] | None = None,
) -> None:
    """Worker process entry point.

    Must stay a module-level function: Windows uses the *spawn* start method, so the target is
    re-imported and looked up by qualified name in the child. A closure or bound method would
    fail to pickle.

    Publishes a :class:`Snapshot` to ``out_q`` every ``poll_interval_ms``, including while
    disconnected, so the supervisor's heartbeat check can distinguish a wedged worker from an
    unreachable broker. Drains ``cmd_q`` between ticks.

    ``_session_factory`` is a test seam; production passes nothing and the real MT5 module is
    imported here.
    """
    configure(Path(log_file))
    wlog = get_logger(f"worker.{terminal_id[:8]}")
    backoff = reconnect_backoff_s or [1.0, 2.0, 5.0, 10.0, 30.0]
    interval = max(poll_interval_ms, 50) / 1000.0

    if _session_factory is None:
        import MetaTrader5 as mt5  # noqa: N813 -- the package's own capitalisation

        def _session_factory(path: Path) -> _Session:
            return _Session(mt5, path)

    session = _session_factory(Path(exe_path))
    executor = Executor(terminal_id, session)
    seq = 0
    attempts = 0
    next_retry_at = 0.0
    running = True
    close_terminal = False
    close_any = False
    """Set from a STOP command's own flag. Stays False for every other way this loop can end
    (a crash, the queue breaking, an unflagged STOP) -- an unplanned exit must never surprise
    the user by closing a terminal they are still looking at."""

    pid = os.getpid()

    def publish(snap: Snapshot) -> None:
        try:
            out_q.put_nowait(replace(snap, worker_pid=pid))
        except Exception as exc:
            # A full queue means the supervisor is not draining; dropping a snapshot is
            # correct, since the next tick supersedes it anyway.
            wlog.debug("could not publish snapshot: %r", exc)

    def publish_result(result: OrderResult) -> None:
        """Order results are **not** droppable the way snapshots are.

        A snapshot supersedes itself next tick; a result is the only record that an order was
        attempted. Losing one leaves the supervisor believing a trade is still pending that
        has in fact already been placed, so this blocks briefly rather than discarding.
        """
        try:
            out_q.put(result, timeout=2.0)
        except Exception as exc:
            wlog.error("could not report order result for #%s: %r", result.master_ticket, exc)

    wlog.info("worker starting for %s", exe_path)
    publish(Snapshot(terminal_id=terminal_id, state=WorkerState.STARTING, seq=seq))

    try:
        while running:
            # --- commands ------------------------------------------------------------------
            while True:
                try:
                    cmd = cmd_q.get_nowait()
                except queue.Empty:
                    break
                except (EOFError, OSError):
                    running = False
                    break
                if cmd is None or getattr(cmd, "kind", None) is CommandKind.STOP:
                    close_terminal = bool(getattr(cmd, "close_terminal", False))
                    close_any = bool(getattr(cmd, "close_any", False))
                    wlog.info(
                        "stop requested (close_terminal=%s, close_any=%s)",
                        close_terminal,
                        close_any,
                    )
                    running = False
                    break
                if isinstance(cmd, WatchCommand):
                    # Latest wins, connected or not: resolution happens lazily in poll(),
                    # once there is a symbol list to resolve against.
                    session.watch = cmd
                    continue
                if isinstance(cmd, RatesRequest):
                    if session.connected:
                        got = session.read_rates(cmd)
                    else:
                        got = "the feed terminal is not connected"
                    answer = RatesResult(
                        terminal_id=terminal_id,
                        symbol=cmd.symbol,
                        timeframe=cmd.timeframe,
                        candles=got if not isinstance(got, str) else (),
                        error=got if isinstance(got, str) else None,
                        ts=time.time(),
                    )
                    try:
                        # Droppable, like a snapshot: the supervisor asks again soon.
                        out_q.put_nowait(answer)
                    except Exception as exc:
                        wlog.debug("could not publish bars: %r", exc)
                    continue
                if isinstance(cmd, OrderCommand):
                    if not session.connected:
                        # Refuse rather than queue: by the time the link returns, the master's
                        # trade is old news and the copy would be a late join. Transient, so
                        # the ledger may derive it again -- which it will for a close or a
                        # modify, whose target state does not go stale, and never for an open.
                        publish_result(
                            OrderResult(
                                command_id=cmd.command_id,
                                terminal_id=terminal_id,
                                op=cmd.op,
                                master_ticket=cmd.master_ticket,
                                ok=False,
                                retryable=True,
                                error="terminal not connected when the command arrived",
                            )
                        )
                        continue
                    result = executor.execute(cmd)
                    wlog.info("%s", result.describe())
                    publish_result(result)
                    continue
                if isinstance(cmd, Command) and cmd.kind is CommandKind.RECONNECT:
                    wlog.info("reconnect requested")
                    session.disconnect()
                    # The broker's symbol list is the one input a cached resolution cannot
                    # notice changing, so a reconnect is the deliberate moment to re-read it.
                    executor.forget_symbols()
                    attempts = 0
                    next_retry_at = 0.0
            if not running:
                break

            # --- connection ----------------------------------------------------------------
            if not session.connected:
                now = time.monotonic()
                if now < next_retry_at:
                    seq += 1
                    publish(
                        Snapshot(
                            terminal_id=terminal_id,
                            state=WorkerState.CONNECTING,
                            seq=seq,
                            error=f"retrying in {next_retry_at - now:.0f}s",
                            connect_attempts=attempts,
                        )
                    )
                    time.sleep(min(interval, max(next_retry_at - now, 0.0)))
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
                ok, err = session.connect()
                if not ok:
                    delay = _backoff_delay(backoff, attempts)
                    next_retry_at = time.monotonic() + delay
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

            # --- poll ----------------------------------------------------------------------
            seq += 1
            snap = session.poll(terminal_id, seq, attempts)
            publish(snap)
            if not session.connected:
                next_retry_at = time.monotonic() + _backoff_delay(backoff, 1)
            time.sleep(interval)

    except KeyboardInterrupt:
        wlog.info("interrupted")
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
        session.disconnect(close_terminal=close_terminal, close_any=close_any)
        seq += 1
        publish(Snapshot(terminal_id=terminal_id, state=WorkerState.STOPPED, seq=seq))
        wlog.info("worker stopped")
