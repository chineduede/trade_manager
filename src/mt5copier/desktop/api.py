"""The JS bridge: everything the desktop window may ask of the engine.

The window is a view, exactly like the TUI screens -- every method here is a thin
translation between JSON-friendly dicts and the engine's own calls, and the rules stay
where they live: the virtual book validates orders, the workers pad targets, the guards
gate copies. Nothing here decides anything about trading.

pywebview calls these from its own worker threads; the engine is lock-guarded, so that is
the same contract the TUI's timer thread already exercises.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .. import config as config_mod
from ..config import AppMode, Role, SizingMode
from ..logging_setup import get_logger
from ..paths import active_profile
from ..protocol import CHART_TIMEFRAMES, OrderType, PendingOrder, Position, PositionType

if TYPE_CHECKING:
    import webview

    from ..config import AppConfig
    from ..engine import Engine

log = get_logger(__name__)

_TIMEFRAMES = dict(CHART_TIMEFRAMES)

_ORDER_TYPES: dict[tuple[str, str], OrderType] = {
    ("buy", "limit"): OrderType.BUY_LIMIT,
    ("buy", "stop"): OrderType.BUY_STOP,
    ("sell", "limit"): OrderType.SELL_LIMIT,
    ("sell", "stop"): OrderType.SELL_STOP,
}

_NOMINAL_LOTS = 1.0
"""What the virtual master's book records per ticket. The ticket has no lots field --
every slave sizes from its own risk budget off the stop distance, and a stop is required
on every direct trade, so the master's volume never reaches a slave's sizing. One lot
keeps partials legible: close 50% and the book row reads 0.50, the fraction left."""


def _position_row(p: Position, bid: float | None, ask: float | None) -> dict[str, Any]:
    now = None
    if bid is not None and ask is not None:
        now = bid if p.type is PositionType.BUY else ask
    return {
        "ticket": p.ticket,
        "open": True,
        "symbol": p.symbol,
        "side": p.type.name.lower(),
        "kind": "market",
        "lots": p.volume,
        "entry": p.price_open,
        "sl": p.sl,
        "tp": p.tp,
        "now": now,
    }


def _order_row(o: PendingOrder, now: float) -> dict[str, Any]:
    left = None
    if o.expiration_utc:
        left = max(0, int((o.expiration_utc - now) // 60))
    return {
        "ticket": o.ticket,
        "open": False,
        "symbol": o.symbol,
        "side": o.type.direction.name.lower(),
        "kind": o.type.name.lower().replace("buy_", "").replace("sell_", ""),
        "lots": o.volume,
        "entry": o.price_open,
        "sl": o.sl,
        "tp": o.tp,
        "expiresMin": left,
    }


class DesktopApi:
    """One instance per window, handed to ``webview.create_window(js_api=...)``."""

    def __init__(self, cfg: AppConfig, engine: Engine) -> None:
        self._cfg = cfg
        self._engine = engine
        self._window: webview.Window | None = None
        self._maximized = False

    def attach(self, window: webview.Window) -> None:
        self._window = window

    # --- window chrome ----------------------------------------------------------------------

    def win_minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def win_toggle_max(self) -> None:
        if self._window is None:
            return
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized

    def win_close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    # --- reading ----------------------------------------------------------------------------

    def state(self, symbol: str, _timeframe: str = "") -> dict[str, Any]:
        """Everything the page redraws from, in one call per poll.

        Asking is part of reading, exactly as with the engine's own accessors: the ticket's
        symbol is put on the feed's watch here, so the quote starts flowing while the person
        is still typing.
        """
        engine = self._engine
        cfg = self._cfg
        symbol = (symbol or "").strip().upper()
        engine.watch_direct(symbol or None)
        quote = engine.direct_quote(symbol) if symbol else None

        feed_tid = engine.direct_feed()
        now = time.time()
        quotes: dict[str, Any] = {}
        positions = engine.direct_positions()
        for p in positions:
            if p.symbol not in quotes:
                quotes[p.symbol] = engine.direct_quote(p.symbol)
        rows = [
            _position_row(
                p,
                quotes[p.symbol].bid if quotes[p.symbol] else None,
                quotes[p.symbol].ask if quotes[p.symbol] else None,
            )
            for p in positions
        ]
        rows += [_order_row(o, now) for o in engine.direct_orders()]

        health = engine.health()
        return {
            "profile": active_profile(),
            "direct": engine.direct_mode(),
            "copying": cfg.copying_enabled,
            "dryRun": cfg.dry_run,
            "slaves": len(cfg.slaves),
            "feed": cfg.display_name(feed_tid) if feed_tid is not None else None,
            "quote": {"bid": quote.bid, "ask": quote.ask} if quote is not None else None,
            "book": rows,
            "sizing": self._sizing(),
            "workersLive": health["live"],
            "workersTotal": health["supervised"],
        }

    def _sizing(self) -> list[dict[str, Any]]:
        """Each enabled slave's name and risk budget, for the ticket's sizing line.

        The budget is exact -- it is the same arithmetic the engine sizes with. The lots the
        page derives from it are an estimate (the slave's own terminal prices the stop
        distance and adds commission), and the page says so.
        """
        out: list[dict[str, Any]] = []
        for view in self._engine.views():
            tcfg = view.cfg
            if tcfg.role is not Role.SLAVE or not tcfg.enabled:
                continue
            budget: float | None
            if tcfg.sizing_mode is SizingMode.FIXED_CASH_RISK:
                budget = tcfg.risk_cash
                said = f"{tcfg.risk_cash:g} cash"
            else:
                balance = view.snapshot.balance if view.snapshot is not None else None
                budget = None if balance is None else balance * tcfg.risk_percent / 100.0
                said = f"{tcfg.risk_percent:g}%"
            out.append(
                {
                    "name": self._cfg.display_name(tcfg.terminal_id),
                    "budget": budget,
                    "risk": said,
                }
            )
        return out

    def rates(self, symbol: str, timeframe: str) -> dict[str, Any]:
        symbol = (symbol or "").strip().upper()
        tf = _TIMEFRAMES.get(timeframe, CHART_TIMEFRAMES[1][1])
        if not symbol:
            return {"candles": [], "error": None, "waiting": False}
        result = self._engine.direct_rates(symbol, tf)
        if result is None:
            return {"candles": [], "error": None, "waiting": True}
        if result.error is not None:
            return {"candles": [], "error": result.error, "waiting": False}
        return {
            "candles": [
                {"time": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
                for c in result.candles
            ],
            "error": None,
            "waiting": False,
        }

    # --- acting -----------------------------------------------------------------------------

    def place(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """Write the page's ticket into the book. The book rules on it, not the page: the
        page's own validation only keeps the button honest, and anything it missed comes
        back here as the refusal to show."""
        try:
            symbol = str(ticket["symbol"]).strip().upper()
            side = str(ticket["side"])
            kind = str(ticket["kind"])
            sl = float(ticket["sl"])
            tp = float(ticket.get("tp") or 0.0)
            entry = float(ticket.get("entry") or 0.0)
            expiry_min = float(ticket.get("expiryMinutes") or 0.0)
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "message": "the ticket did not read; check its numbers"}
        direction = PositionType.BUY if side == "buy" else PositionType.SELL
        if kind == "market":
            why = self._engine.place_direct_market(symbol, direction, _NOMINAL_LOTS, sl, tp)
        else:
            order_type = _ORDER_TYPES.get((side, kind))
            if order_type is None:
                return {"ok": False, "message": f"unknown order kind {kind!r}"}
            expiration = int(time.time() + expiry_min * 60) if expiry_min > 0 else 0
            why = self._engine.place_direct_pending(
                symbol, order_type, _NOMINAL_LOTS, entry, sl, tp, expiration
            )
        if why is not None:
            return {"ok": False, "message": why}
        what = "Opened" if kind == "market" else "Resting"
        return {"ok": True, "message": f"{what} {symbol}; copies follow on the next tick."}

    def close(self, ticket: int) -> dict[str, Any]:
        return self._did(self._engine.close_direct(int(ticket)), f"Closed #{ticket}")

    def cancel(self, ticket: int) -> dict[str, Any]:
        return self._did(self._engine.cancel_direct(int(ticket)), f"Cancelled #{ticket}")

    def modify_position(self, ticket: int, sl: float, tp: float) -> dict[str, Any]:
        why = self._engine.modify_direct(int(ticket), float(sl), float(tp or 0.0))
        return self._did(why, f"Moved #{ticket}")

    def modify_order(self, ticket: int, entry: float, sl: float, tp: float) -> dict[str, Any]:
        ticket = int(ticket)
        order = next((o for o in self._engine.direct_orders() if o.ticket == ticket), None)
        expiry = order.expiration_utc if order is not None else 0
        why = self._engine.modify_direct_order(
            ticket, float(entry), float(sl), float(tp or 0.0), expiry
        )
        return self._did(why, f"Moved #{ticket}")

    def toggle_halt(self) -> dict[str, Any]:
        """The title bar's one button: stop everything copying, or let it again."""
        cfg = self._cfg
        if not cfg.copying_enabled:
            if not cfg.slaves:
                return {"ok": False, "message": "no slaves to copy to; assign one first"}
            if cfg.mode is not AppMode.TRADING and cfg.master is None:
                return {"ok": False, "message": "no master designated; this profile copies one"}
            cfg.copying_enabled = True
        else:
            cfg.copying_enabled = False
        try:
            config_mod.save(cfg)
        except OSError as exc:
            log.exception("config save failed")
            cfg.copying_enabled = not cfg.copying_enabled
            return {"ok": False, "message": f"could not save config: {exc}"}
        self._engine.sync()
        return {"ok": True, "message": "Copying resumed." if cfg.copying_enabled else "Halted."}

    @staticmethod
    def _did(why: str | None, done: str) -> dict[str, Any]:
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"{done}; copies follow on the next tick."}
