"""The JS bridge: everything the desktop window may ask of the engine.

The window is a view, exactly like the TUI screens -- every method here is a thin
translation between JSON-friendly dicts and the engine's own calls, and the rules stay
where they live: the virtual book validates orders, the workers pad targets, the guards
gate copies. Nothing here decides anything about trading.

pywebview calls these from its own worker threads; the engine is lock-guarded, so that is
the same contract the TUI's timer thread already exercises.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from .. import account_switch, role_actions, shared_data
from .. import config as config_mod
from .. import profiles as profiles_mod
from .. import setups as setups_mod
from ..config import (
    AppMode,
    BreakevenMode,
    Management,
    NoStopPolicy,
    Role,
    SizingMode,
    TerminalConfig,
    account_key,
    record_login,
)
from ..copy_stats import TerminalStats
from ..ctrader import auth as ctrader_auth
from ..day_book import MasterEvent, Outcome
from ..discovery import discover_terminals
from ..general_edit import (
    APPLIES_ON_CONNECT,
    DRY_RUN,
    GENERAL,
    apply_general,
    commit_general,
    initial_general,
)
from ..guards import REGISTRY, GuardRule, GuardsDoc, Level
from ..guards import model as guards_model
from ..guards.base import money
from ..guards.params import ZONE_HINT, ParamKind, ParamValue
from ..history import NOTE
from ..log_entries import (
    LogEntry,
    event_entries,
    file_entries,
    merge,
    search,
    search_words,
    select,
    terminal_names,
)
from ..logging_setup import get_logger
from ..news import HIGH, MEDIUM, symbol_currencies
from ..paths import active_profile, guards_path, is_frozen, log_dir, setups_path
from ..presentation import read_log_tail, row_info, slave_badge
from ..protocol import (
    ACCOUNT_TRADE_MODE_DEMO,
    CHART_TIMEFRAMES,
    COPIER_MAGIC,
    ActionKind,
    OrderType,
    PendingOrder,
    Position,
    PositionType,
    Snapshot,
)
from ..settings_edit import (
    BY_NAME,
    advisories,
    apply_edits,
    copies_word,
    describe_changes,
    initial_text,
    initial_values,
    rename,
)
from ..settings_example import MasterTrade, PriceValue, pick, work
from ..setups import Setup, SetupsDoc
from ..symbols import base_name, undecorate
from ..terminal_detail import (
    PositionRow,
    breakeven_rule,
    exposure,
    format_duration,
    format_ping,
    held_by_counts,
    margin_gauge,
    position_rows,
    price_digits,
)
from . import winframe

if TYPE_CHECKING:
    import webview

    from ..config import AppConfig
    from ..engine import Engine, TerminalView
    from ..guards import RuleStatus
    from ..history import GuardAction, Step, Trade
    from ..news import NewsEvent
    from ..reconcile import OpenCopy

log = get_logger(__name__)

_TIMEFRAMES = dict(CHART_TIMEFRAMES)

_ORDER_TYPES: dict[tuple[str, str], OrderType] = {
    ("buy", "limit"): OrderType.BUY_LIMIT,
    ("buy", "stop"): OrderType.BUY_STOP,
    ("sell", "limit"): OrderType.SELL_LIMIT,
    ("sell", "stop"): OrderType.SELL_STOP,
}

_LOG_LINES = 500
"""How much of ``copier.log`` the log dock reads: the TUI log page's own depth."""

_LOG_SHOWN = 300
"""The most recent entries a poll answers with; the dock says when it held more back."""

_NOMINAL_LOTS = 1.0
"""What the virtual master's book records per ticket. The ticket has no lots field --
every slave sizes from its own risk budget off the stop distance, and a stop is required
on every direct trade, so the master's volume never reaches a slave's sizing. One lot
keeps partials legible: close 50% and the book row reads 0.50, the fraction left."""


def _position_row(
    p: Position, bid: float | None, ask: float | None, copies: list[dict[str, str]]
) -> dict[str, Any]:
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
        # The ticket is intent; this is what still stands behind it on the slaves,
        # from engine.direct_copy_status: per copy, live / closed / unknown.
        "copies": copies,
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

    def __init__(
        self,
        cfg: AppConfig,
        engine: Engine,
        guards: GuardsDoc | None = None,
        setups: SetupsDoc | None = None,
    ) -> None:
        self._cfg = cfg
        self._engine = engine
        # The same document the engine enforces from (see tui.run and desktop.run): edits
        # here are one assignment into it, and the file is written after, as the TUI does.
        self._guards_doc = guards if guards is not None else GuardsDoc()
        self._setups = setups if setups is not None else SetupsDoc()
        self._window: webview.Window | None = None
        self._maximized = False
        self._setup_busy = False
        """Whether a setup is being loaded right now; its logins block that bridge thread
        for a while, and a second load started under it would race the first."""
        self._logging_in: set[str] = set()
        """Terminals an account login is running for, each blocking its own bridge thread;
        asking for a second login on one is refused while the first still runs."""
        self._ctrader_cancel: threading.Event | None = None
        """Set while a cTrader login waits for the browser, blocking its bridge thread.
        Logging in again gives that one up first, the TUI t key's own behaviour."""
        self._log_cache: tuple[object, list[LogEntry]] = (None, [])
        """``copier.log`` as entries, with what the file looked like when it was read."""
        self._switchable: dict[str, tuple[float, bool]] = {}
        """Per terminal, (when it was checked, whether another saved account exists).
        Reading the account list walks the terminal's data folder; the cards poll every
        second, so the walk is paced here and dropped after a login changes the answer."""
        self._busy_lock = threading.Lock()
        self._guards_stamp: float | None = None
        """``guards.json``'s mtime as last read or written here. The file is shared by every
        profile, so a poll can tell an edit made in another window from nothing by one stat."""

    def attach(self, window: webview.Window) -> None:
        self._window = window

    def _refresh_shared(self) -> None:
        """Fold in what another profile's window saved since the last poll: account names
        (into the config) and guard rules (into the live doc, the file winning per key --
        every local edit saves at once, so memory never holds anything newer than the file).
        Called from the polls that read them."""
        shared_data.refresh_names(self._cfg)
        try:
            stamp = guards_path().stat().st_mtime
        except OSError:
            return
        if stamp == self._guards_stamp:
            return
        self._guards_stamp = stamp
        fresh = guards_model.load(guards_path())
        self._guards_doc.accounts.update(fresh.accounts)
        self._guards_doc.templates.update(fresh.templates)

    # --- window chrome ----------------------------------------------------------------------

    def _hwnd(self) -> int | None:
        try:
            if self._window is None:
                return None
            # ``native`` is the WinForms form; pywebview types it as None until shown.
            return int(self._window.native.Handle.ToInt64())  # type: ignore[attr-defined]
        except Exception:
            return None

    def win_minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def _on_ui(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the thread that owns the window. The bridge calls arrive on
        worker threads, and both ReleaseCapture and the modal move loop belong to the UI
        thread; BeginInvoke rather than Invoke, so a drag's whole move loop does not hold
        a bridge worker hostage."""
        try:
            from System import Action  # pythonnet, loaded with webview

            self._window.native.BeginInvoke(Action(fn))  # type: ignore[union-attr]
        except Exception:
            log.debug("could not reach the UI thread", exc_info=True)

    def win_drag(self) -> None:
        """The title bar's drag, as a native move loop: fluid, and it snaps at edges."""
        hwnd = self._hwnd()
        if hwnd is not None:
            self._on_ui(lambda: winframe.begin_drag(hwnd))

    def win_resize(self, edge: str) -> None:
        hwnd = self._hwnd()
        edge = str(edge)
        if hwnd is not None:
            self._on_ui(lambda: winframe.begin_resize(hwnd, edge))

    def _is_maximized(self) -> bool:
        """The window's real state: a drag to the top edge maximizes natively, so a flag
        of our own would go stale the first time that happens."""
        try:
            from System.Windows.Forms import FormWindowState  # pythonnet, loaded with webview

            return bool(self._window.native.WindowState == FormWindowState.Maximized)  # type: ignore[union-attr]
        except Exception:
            return self._maximized

    def win_toggle_max(self) -> None:
        if self._window is None:
            return
        if self._is_maximized():
            self._window.restore()
            self._maximized = False
        else:
            self._fit_maximized_bounds()
            self._window.maximize()
            self._maximized = True

    def _fit_maximized_bounds(self) -> None:
        """Make maximize behave like a titled window's, taskbar and all.

        pywebview's Windows backend is a borderless WinForms form, and a borderless form
        maximizes over the whole screen rather than the working area. The shell then treats
        it as a fullscreen app and suppresses the taskbar -- even an auto-hidden one stops
        revealing on hover. Capping ``MaximizedBounds`` at the monitor's working area
        restores the normal behaviour; with an auto-hidden taskbar the working area IS the
        whole screen, so a two-pixel seam is left at the bottom to keep the shell from
        calling it fullscreen. Recomputed on every maximize, because the window may have
        been dragged to another monitor since the last one.
        """
        try:
            from System import Action  # pythonnet namespaces, importable once webview is up
            from System.Drawing import Rectangle
            from System.Windows.Forms import Screen

            form = self._window.native if self._window is not None else None
            if form is None:
                return

            def apply() -> None:
                screen = Screen.FromControl(form)
                area = screen.WorkingArea
                if area == screen.Bounds:
                    area = Rectangle(area.X, area.Y, area.Width, area.Height - 2)
                form.MaximizedBounds = area

            form.Invoke(Action(apply))
        except Exception as exc:
            log.debug("could not fit the maximized bounds: %r", exc)

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
        statuses = engine.direct_copy_status()
        rows = [
            _position_row(
                p,
                quotes[p.symbol].bid if quotes[p.symbol] else None,
                quotes[p.symbol].ask if quotes[p.symbol] else None,
                statuses.get(p.ticket, []),
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
            "guardEvents": self._guard_events(now),
            "problemEvents": self._problem_events(now),
            "workersLive": health["live"],
            "workersTotal": health["supervised"],
        }

    def _guard_events(self, now: float) -> list[dict[str, Any]]:
        """The last couple of minutes of guard actions on any account, so the page can
        toast each one as it happens: a declined copy must say so where the trade was
        placed, not wait on the Guards page until the person goes looking."""
        out: list[dict[str, Any]] = []
        for a in self._engine.recent_guard_actions(now - 120.0):
            out.append(
                {
                    "key": f"{a.ts:.3f}|{a.terminal_id}|{a.rule_id}|{a.action}|{a.master_ticket}",
                    "slave": self._cfg.display_name(a.terminal_id),
                    "rule": a.rule,
                    "action": a.action,
                    "trade": f"#{a.master_ticket} {a.symbol} {a.label}".strip()
                    if a.master_ticket is not None
                    else "",
                    "detail": a.detail,
                }
            )
        return out

    def _problem_events(self, now: float) -> list[dict[str, Any]]:
        """The last couple of minutes of plain per-slave failures -- a broker refusal, a
        link failure, a copy never answered -- with no guard behind them, so each one
        toasts where the person is instead of only reaching the log. (A minimum-lot
        sizing refusal was the live case that only the log saw.)"""
        out: list[dict[str, Any]] = []
        for s in self._engine.recent_problems(now - 120.0):
            out.append(
                {
                    "key": f"{s.ts:.3f}|{s.terminal_id}|{s.master_ticket}|{s.action}|{s.outcome}",
                    "slave": self._cfg.display_name(s.terminal_id),
                    "did": s.action,
                    "trade": f"#{s.master_ticket} {s.symbol} {s.label}".strip(),
                    "detail": s.slave,
                }
            )
        return out

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

    def symbol_search(self, query: str) -> dict[str, Any]:
        """Symbols matching what is typed, from the feed broker's own catalog (pushed once
        per connection by its worker), as clean base names: what every slave resolves from.
        Prefix matches first, then anywhere-in-the-name; a dozen at most."""
        q = str(query).strip().upper()
        names: list[str] = []
        for raw in self._engine.feed_symbols():
            clean = base_name(raw).upper()
            if clean not in names:
                names.append(clean)
        if not q:
            return {"symbols": names[:12], "known": len(names)}
        starts = [n for n in names if n.startswith(q)]
        contains = [n for n in names if q in n and not n.startswith(q)]
        return {"symbols": (starts + contains)[:12], "known": len(names)}

    def close(self, ticket: int) -> dict[str, Any]:
        return self._did(self._engine.close_direct(int(ticket)), f"Closed #{ticket}")

    def cancel(self, ticket: int) -> dict[str, Any]:
        return self._did(self._engine.cancel_direct(int(ticket)), f"Cancelled #{ticket}")

    def partial_close(self, ticket: int, percent: Any) -> dict[str, Any]:
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            return {"ok": False, "message": "give the percent to close, such as 50"}
        why = self._engine.partial_close_direct(int(ticket), pct)
        return self._did(why, f"Closed {pct:g}% of #{ticket}")

    def flatten(self) -> dict[str, Any]:
        """Close every position and cancel every resting order; the page confirms first."""
        count = self._engine.flatten_direct()
        if count == 0:
            return {"ok": False, "message": "nothing was open or resting"}
        return {
            "ok": True,
            "message": f"closed everything: {count} position(s) and order(s); "
            "copies follow on the next tick.",
        }

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

    # --- the terminals page -----------------------------------------------------------------

    def _persist(self) -> str | None:
        """Save the config and wake the engine, exactly as the TUI's _persist_and_sync does.
        Returns why the save failed, or None; the engine syncs either way, so what the person
        was told happened is at least true for this run."""
        why: str | None = None
        try:
            config_mod.save(self._cfg)
        except OSError as exc:
            log.exception("config save failed")
            why = f"could not save config: {exc}"
        self._engine.sync()
        return why

    def _guard_info(self, terminal_id: str) -> dict[str, Any]:
        """The card's guard corner: the TUI's guard_badge wording, plus how many rules are on."""
        report = self._engine.guard_report(terminal_id)
        live = [r for r in report.rules if r.level is not Level.OFF]
        text, tone = "", ""
        if report.holding_back:
            text, tone = "GUARD: NO NEW TRADES", "red"
        else:
            worst = report.worst
            if worst is not None and worst.level is not Level.OK:
                if worst.level is Level.BREACHED:
                    text, tone = "GUARD BREACHED", "red"
                else:
                    said = f" {worst.fraction:.0%}" if worst.fraction is not None else ""
                    text, tone = f"GUARD{said}", "yellow"
        return {"rules": len(live), "text": text, "tone": tone}

    def _card(self, view: Any, holds: dict[str, str]) -> dict[str, Any]:
        cfg = self._cfg
        tcfg = view.cfg
        tid = tcfg.terminal_id
        info = row_info(
            view,
            timeout_s=cfg.heartbeat_timeout_s,
            hold_reason=holds.get(tid),
            display_name=cfg.display_name(tid),
        )
        is_slave = tcfg.role is Role.SLAVE
        badge_text, badge_tier = slave_badge(info, cfg.dry_run) if is_slave else ("", "")
        snap = view.snapshot
        margin_used: float | None = None
        margin_tier: str | None = None
        if snap is not None:
            gauge = margin_gauge(snap)
            margin_used = gauge.used_fraction
            margin_tier = gauge.level_tier
        risk: str | None = None
        if is_slave:
            # The TUI card's wording (cards.risk_text): $ only where it reads naturally.
            if tcfg.sizing_mode is SizingMode.FIXED_CASH_RISK:
                mark = (
                    "$" if (info.currency or "").upper() in ("USD", "AUD", "CAD", "NZD", "") else ""
                )
                risk = f"risk {mark}{tcfg.risk_cash:,g}" + ("" if mark else f" {info.currency}")
            else:
                risk = f"risk {tcfg.risk_percent:g}%"
        key = account_key(tcfg.last_login, tcfg.last_server)
        return {
            "tid": tid,
            "name": info.label,
            "accountName": cfg.account_names.get(key) if key is not None else None,
            "role": str(tcfg.role),
            "statusText": info.status_text,
            # The full sentence rides separately: the card's state word stays short and the
            # info line carries the story ("the terminal is in use by profile 'x'").
            "claimRefused": view.claim_refused,
            "tier": info.tier,
            "login": info.login,
            "server": info.server,
            "balance": info.balance,
            "equity": info.equity,
            "floatingPl": info.floating_pl,
            "currency": info.currency,
            "openTrades": info.open_trades,
            "paused": info.paused,
            "holdReason": info.hold_reason,
            "demoBlocked": info.demo_guard_blocked,
            "badge": badge_text,
            "badgeTier": badge_tier,
            "isCtrader": tcfg.is_ctrader,
            "hasExe": tcfg.exe_path is not None,
            "loggingIn": tid in self._logging_in,
            "canSwitch": self._can_switch_account(tcfg),
            "usable": tcfg.usable,
            "enabled": tcfg.enabled,
            "allowLive": tcfg.allow_live_trading,
            "orphans": tid in holds and bool(self._engine.orphans(tid)),
            "guard": self._guard_info(tid) if is_slave else None,
            "risk": risk,
            "marginUsed": margin_used,
            "marginTier": margin_tier,
        }

    def terminals_state(self) -> dict[str, Any]:
        """Everything the Terminals page redraws from, in one call per poll."""
        self._refresh_shared()
        engine = self._engine
        cfg = self._cfg
        holds = engine.holds()
        master: dict[str, Any] | None = None
        slaves: list[dict[str, Any]] = []
        detected: list[dict[str, Any]] = []
        for view in engine.views():
            card = self._card(view, holds)
            if view.cfg.role is Role.MASTER:
                master = card
            elif view.cfg.role is Role.SLAVE:
                slaves.append(card)
            else:
                detected.append(card)
        virtual: dict[str, Any] | None = None
        if cfg.mode is AppMode.TRADING:
            feed_tid = engine.direct_feed()
            virtual = {
                "feed": cfg.display_name(feed_tid) if feed_tid is not None else None,
                "positions": len(engine.direct_positions()),
                "orders": len(engine.direct_orders()),
            }
        return {
            "master": master,
            "virtual": virtual,
            "slaves": slaves,
            "detected": detected,
            "dryRun": cfg.dry_run,
            "copying": cfg.copying_enabled,
            "hasMaster": virtual is not None or master is not None,
        }

    def assign_master(self, tid: str) -> dict[str, Any]:
        tid = str(tid)
        if tid not in self._cfg.terminals:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        refusal = role_actions.set_master(self._cfg, tid)
        if refusal is not None:
            return {"ok": False, "message": refusal}
        why = self._persist()
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"master: {self._cfg.display_name(tid)}"}

    def assign_slave(self, tid: str) -> dict[str, Any]:
        tid = str(tid)
        if tid not in self._cfg.terminals:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        refusal = role_actions.set_slave(self._cfg, tid)
        if refusal is not None:
            return {"ok": False, "message": refusal}
        why = self._persist()
        if why is not None:
            return {"ok": False, "message": why}
        name = self._cfg.display_name(tid)
        return {"ok": True, "message": f"{name} is now a slave; it connects on the next tick."}

    def unassign_stake(self, tid: str) -> dict[str, Any]:
        """What unassigning would give up, for the page to show before anything happens.
        The engine checks again at the moment of commitment; this is only the asking."""
        tid = str(tid)
        if tid not in self._cfg.terminals:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        stake = self._engine.copies_at_stake(tid)
        if stake is None:
            return {
                "ok": False,
                "message": "its open copies could not be read, so they cannot be given up safely",
            }
        out = [
            {
                "tid": slave_tid,
                "name": self._cfg.display_name(slave_tid),
                "copies": [
                    {
                        "masterTicket": c.master_ticket,
                        "slaveTicket": c.slave_ticket,
                        "symbol": c.symbol,
                        "volume": c.volume,
                        "resting": c.resting,
                    }
                    for c in held
                ],
            }
            for slave_tid, held in stake.items()
        ]
        return {"ok": True, "stake": out}

    def unassign(self, tid: str, agreed: dict[str, list[int]] | None = None) -> dict[str, Any]:
        """Commit the unassign. ``agreed`` is what the person was shown and said yes to,
        per slave the master tickets, exactly as the engine re-checks it."""
        tid = str(tid)
        if tid not in self._cfg.terminals:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        name = self._cfg.display_name(tid)
        was_master = self._cfg.terminals[tid].role is Role.MASTER
        swept = [t.terminal_id for t in self._cfg.slaves] if was_master else []
        orphan = (
            {str(t): [int(n) for n in tickets] for t, tickets in agreed.items()} if agreed else None
        )
        refusal = self._engine.unassign(tid, orphan=orphan)
        if refusal is not None:
            return {"ok": False, "message": f"{name} was not unassigned: {refusal}"}
        why = self._persist()
        if why is not None:
            return {"ok": False, "message": why}
        message = f"{name} unassigned."
        if swept:
            names = ", ".join(self._cfg.display_name(t) for t in swept)
            message = f"Master unassigned; also unassigned slave(s) left with no master: {names}"
        if orphan:
            total = sum(len(t) for t in orphan.values())
            message += f" {total} cop{'y' if total == 1 else 'ies'} orphaned: they stay open and are no longer managed."
        return {"ok": True, "message": message}

    def rename_account(self, tid: str, name: str) -> dict[str, Any]:
        """Name (or, given blank, unname) the account this terminal is last known to be logged
        into. Cosmetic and immediate -- no copies are at stake, as
        :func:`mt5copier.settings_edit.rename` itself explains -- and kept by account, not
        terminal, so the name follows a login to another terminal."""
        tid = str(tid)
        if tid not in self._cfg.terminals:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        tcfg = self._cfg.terminals[tid]
        key = account_key(tcfg.last_login, tcfg.last_server)
        refusal = rename(self._cfg, tid, str(name or ""))
        if refusal is not None:
            return {"ok": False, "message": refusal}
        why = self._persist()
        if why is not None:
            return {"ok": False, "message": why}
        stripped = str(name or "").strip()
        if stripped:
            return {"ok": True, "message": f"{key} is now called {stripped}."}
        return {
            "ok": True,
            "message": f"The name for {key} is cleared; it shows as its login again.",
        }

    def set_pause(self, tid: str, paused: bool) -> dict[str, Any]:
        tid = str(tid)
        if tid not in self._cfg.terminals:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        name = self._cfg.display_name(tid)
        try:
            self._engine.set_paused(tid, bool(paused))
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        why = self._persist()
        if why is not None:
            return {"ok": False, "message": why}
        if paused:
            log.warning("%s paused: no new trades will be copied to it", name)
            return {
                "ok": True,
                "message": f"{name} paused: no new trades are copied to it. "
                "Copies it already holds are still closed, reduced and kept in sync.",
            }
        log.warning("%s resumed", name)
        return {
            "ok": True,
            "message": f"{name} resumed: trades opened from now on are copied. "
            "Any opened while paused are not.",
        }

    def reconnect(self, tid: str) -> dict[str, Any]:
        self._engine.reconnect(str(tid))
        return {"ok": True, "message": "Reconnect requested."}

    def restart_worker(self, tid: str) -> dict[str, Any]:
        self._engine.restart(str(tid))
        return {"ok": True, "message": "Worker restarted."}

    def acknowledge_orphans(self, tid: str) -> dict[str, Any]:
        tid = str(tid)
        if tid not in self._cfg.terminals:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        name = self._cfg.display_name(tid)
        count = self._engine.acknowledge_orphans(tid)
        if count == 0:
            return {"ok": True, "message": f"{name} has no unacknowledged orphans."}
        return {
            "ok": True,
            "message": f"Acknowledged {count} orphan(s) on {name}. They stay open and "
            "untracked; anything the master opened while it was held is not copied to it.",
        }

    def live_override(self, tid: str, allow: bool) -> dict[str, Any]:
        """The demo-only guard's override: deliberate, logged as a warning, like the TUI's
        LiveDialog. The page asks before calling this; the engine applies it atomically."""
        tid = str(tid)
        if tid not in self._cfg.terminals:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        old = self._cfg.terminals[tid]
        allow = bool(allow)
        name = self._cfg.display_name(tid)
        if old.allow_live_trading == allow:
            return {"ok": True, "message": f"{name}: already so."}
        new: TerminalConfig = old.model_copy(update={"allow_live_trading": allow})
        refusal = self._engine.apply_settings(new)
        if refusal is not None:
            return {"ok": False, "message": refusal}
        log.warning(
            "settings changed for %s (%s): %s",
            name,
            tid[:8],
            "live trading allowed (demo-only guard off)"
            if allow
            else "live trading disallowed (demo-only guard back on)",
        )
        why = self._persist()
        if why is not None:
            return {"ok": False, "message": why}
        if allow:
            return {
                "ok": True,
                "message": f"{name}: live trading allowed. Real orders will be sent to this "
                "account whenever copying is live.",
            }
        return {"ok": True, "message": f"{name}: demo-only guard back on."}

    def rescan(self) -> dict[str, Any]:
        """Passive discovery, on the bridge's own worker thread -- the page awaits the
        promise. Launches no terminals and opens no connections."""
        try:
            found = discover_terminals()
        except Exception as exc:
            log.exception("discovery failed")
            return {"ok": False, "message": f"discovery failed: {exc}"}
        before = set(self._cfg.terminals)
        config_mod.merge_discovery(self._cfg, found)
        new = set(self._cfg.terminals) - before
        why = self._persist()
        if why is not None:
            return {"ok": False, "message": why}
        message = f"{len(found)} terminals found"
        if new:
            message += f", {len(new)} new"
        return {"ok": True, "message": message + "."}

    # --- account login ------------------------------------------------------------------------
    # The TUI's i flow: an unassigned MT5 terminal is logged into one of the accounts that
    # have connected in it, with the password the terminal itself saved. Only unassigned --
    # a terminal with a role holds copies made on the account it is on, and unassigning
    # first is what asks about those (see mt5copier.account_switch).

    def _can_switch_account(self, tcfg: TerminalConfig) -> bool:
        """Whether the card offers a login at all: only an unassigned MT5 terminal, and
        only when a saved account other than the one it is on exists -- with a single
        account there is nothing to pick, so the option would only puzzle."""
        if tcfg.role is not Role.UNASSIGNED or tcfg.is_ctrader or tcfg.exe_path is None:
            return False
        tid = tcfg.terminal_id
        now = time.time()
        hit = self._switchable.get(tid)
        if hit is not None and now - hit[0] < 15.0:
            return hit[1]
        on = account_key(tcfg.last_login, tcfg.last_server)
        can = any(account_key(a.login, a.server) != on for a in account_switch.known_accounts(tid))
        self._switchable[tid] = (now, can)
        return can

    def _account_login_refused(self, tid: str) -> str | None:
        """Why this terminal cannot be logged into another account right now, or None."""
        tcfg = self._cfg.terminals.get(tid)
        if tcfg is None:
            return "that terminal is gone; rescan"
        name = self._cfg.display_name(tid)
        if tcfg.role is not Role.UNASSIGNED:
            return (
                f"unassign {name} first, then change the account: its settings and copies "
                "belong to the account it is logged into, and unassigning asks about them"
            )
        if tcfg.is_ctrader:
            return f"{name} is a cTrader account: its login is the cTrader login on Terminals"
        if tcfg.exe_path is None:
            return f"{name} has no terminal executable"
        with self._busy_lock:
            if tid in self._logging_in:
                return f"{name} is already logging in"
        return None

    def _account_used_by(self, tid: str, key: str) -> str | None:
        """The name of another terminal now on this account, or None. One account is used
        on one terminal at a time: logged into two, each terminal's worker would see the
        same positions and the copier would act on them twice."""
        for other_tid, other in self._cfg.terminals.items():
            if other_tid != tid and account_key(other.last_login, other.last_server) == key:
                return self._cfg.display_name(other_tid)
        return None

    def account_login_ask(self, tid: str) -> dict[str, Any]:
        """The accounts this terminal can be logged into: every one that has ever connected
        in it, read from its own data folder without connecting."""
        tid = str(tid)
        why = self._account_login_refused(tid)
        if why is not None:
            return {"ok": False, "why": why}
        cfg = self._cfg
        name = cfg.display_name(tid)
        accounts = account_switch.known_accounts(tid)
        if not accounts:
            return {
                "ok": False,
                "why": f"no accounts found for {name}: log in to one from MT5 first",
            }
        tcfg = cfg.terminals[tid]
        now = account_key(tcfg.last_login, tcfg.last_server)
        rows: list[dict[str, Any]] = []
        for acct in accounts:
            key = account_key(acct.login, acct.server) or ""
            called = cfg.account_names.get(key)
            rows.append(
                {
                    "key": key,
                    "words": f"{called} · {key}" if called else key,
                    "now": key == now,
                    "usedBy": self._account_used_by(tid, key),
                }
            )
        return {"ok": True, "name": name, "accounts": rows}

    def account_login(self, tid: str, key: str) -> dict[str, Any]:
        """Log the terminal into the picked account, blocking this bridge thread as a setup
        load's logins do -- the page's promise narrates while the other polls keep arriving.
        MT5 uses the password saved in the terminal; none is handled here."""
        tid, key = str(tid), str(key)
        cfg = self._cfg
        why = self._account_login_refused(tid)
        if why is not None:
            return {"ok": False, "message": why}
        name = cfg.display_name(tid)
        acct = next(
            (
                a
                for a in account_switch.known_accounts(tid)
                if account_key(a.login, a.server) == key
            ),
            None,
        )
        if acct is None:
            return {"ok": False, "message": f"{name} has no saved account {key}; rescan"}
        tcfg = cfg.terminals[tid]
        if account_key(tcfg.last_login, tcfg.last_server) == key:
            return {
                "ok": False,
                "message": f"{name} is already logged in to {self._account_called(key)}",
            }
        used_by = self._account_used_by(tid, key)
        if used_by is not None:
            return {
                "ok": False,
                "message": f"{self._account_called(key)} is in use by {used_by}: an account "
                "is used on one terminal at a time",
            }
        with self._busy_lock:
            if tid in self._logging_in:
                return {"ok": False, "message": f"{name} is already logging in"}
            self._logging_in.add(tid)
        try:
            result = self._log_in(tid, acct)
        finally:
            with self._busy_lock:
                self._logging_in.discard(tid)
        called = self._account_called(key)
        if not result.ok:
            return {"ok": False, "message": f"{name} was not logged in to {called}: {result.said}"}
        # Keep the account the login left the terminal on, as the one it was last on --
        # record_login also withdraws the live override when a known account was left.
        tcfg = cfg.terminals[tid]
        cfg.replace_terminal(record_login(tcfg, result.login, result.server or acct.server))
        self._switchable.pop(tid, None)  # the account it is on changed; re-answer fresh
        why = self._persist()
        message = f"{name} is logged in to {called}"
        if why is not None:
            message += f" — {why}"
        return {"ok": True, "message": message}

    # --- the cTrader login --------------------------------------------------------------------

    def ctrader_login_state(self) -> dict[str, Any]:
        """What the login dialog fills from. The saved secret never reaches the page:
        ``hasSecret`` says one exists, and a blank secret on login keeps using it."""
        creds = ctrader_auth.load()
        return {
            "clientId": creds.client_id,
            "redirectUri": creds.redirect_uri,
            "hasSecret": bool(creds.client_secret),
            "loggedIn": creds.logged_in,
            "accounts": len(creds.accounts),
            "waiting": self._ctrader_cancel is not None,
        }

    def ctrader_login(self, client_id: str, secret: str, redirect: str) -> dict[str, Any]:
        """The TUI t flow, whole, on this bridge thread: save the app's details, open the
        browser on cTrader's login, wait for the redirect, exchange the code, list the
        allowed accounts and fold them into the terminals. Blocks for as long as the person
        takes in the browser; asked again meanwhile, the waiting login is given up first."""
        client_id = str(client_id).strip()
        secret = str(secret).strip()
        redirect = str(redirect).strip()
        creds = ctrader_auth.load()
        if not secret:
            secret = creds.client_secret
        if not client_id or not secret:
            return {"ok": False, "message": "The client id and the secret are both needed."}
        if not redirect.startswith(("http://127.0.0.1:", "http://localhost:")):
            return {
                "ok": False,
                "message": "The redirect URI must be http://127.0.0.1:<port>/... or localhost.",
            }
        creds = creds.model_copy(
            update={"client_id": client_id, "client_secret": secret, "redirect_uri": redirect}
        )
        ctrader_auth.save(creds)
        with self._busy_lock:
            waiting = self._ctrader_cancel
            if waiting is not None:
                waiting.set()
            cancel = threading.Event()
            self._ctrader_cancel = cancel
        try:
            accounts = ctrader_auth.log_in(creds, cancel=cancel)
        except ctrader_auth.LoginCancelled:
            return {"ok": False, "message": "the waiting cTrader login was given up"}
        except ctrader_auth.LoginError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            log.exception("cTrader login failed")
            return {"ok": False, "message": f"the login failed: {exc!r}"}
        finally:
            with self._busy_lock:
                if self._ctrader_cancel is cancel:
                    self._ctrader_cancel = None
        added = config_mod.merge_ctrader_accounts(
            self._cfg, [(a.account_id, a.is_live, a.login, a.broker) for a in accounts]
        )
        why = self._persist()
        # A worker reads the token when it connects; one already running has the old one.
        for tcfg in self._cfg.assigned():
            if tcfg.is_ctrader:
                self._engine.reconnect(tcfg.terminal_id)
        if not accounts:
            return {"ok": False, "message": "cTrader login done, but no account was ticked"}
        msg = f"cTrader: {len(accounts)} account{'s' if len(accounts) != 1 else ''} allowed"
        if added:
            msg += f", {len(added)} new under DETECTED: drag one into SLAVES"
        if why is not None:
            msg += f" — {why}"
        return {"ok": True, "message": msg}

    # --- profiles -----------------------------------------------------------------------------

    def profiles_state(self) -> dict[str, Any]:
        """The TUI picker's rows. profiles.list_profiles never reads a config through the
        app's own validation, so a profile this build would refuse still shows."""
        me = active_profile()
        return {
            "current": me,
            "profiles": [
                {
                    "name": p.name,
                    "isDefault": p.is_default,
                    "current": p.name == me,
                    "running": p.running,
                    "pid": p.pid,
                    "master": p.master,
                    "trading": p.trading,
                    "slaves": p.slaves,
                    "lastOpened": _profile_age(p.last_opened),
                }
                for p in profiles_mod.list_profiles()
            ],
        }

    def profile_open(self, name: str) -> dict[str, Any]:
        """Open a profile in its own window: a new copier process, detached so it outlives
        this one. The single-instance lock in that process still has the last word."""
        name = str(name).strip()
        info = next((p for p in profiles_mod.list_profiles() if p.name == name), None)
        if info is None:
            return {"ok": False, "message": f"no profile called {name!r}"}
        if name == active_profile():
            return {"ok": False, "message": f"{name!r} is this window"}
        if info.running:
            return {"ok": False, "message": f"{name!r} is already running (pid {info.pid})"}
        if is_frozen():
            cmd = [sys.executable, "--desktop", "--profile", name]
        else:
            cmd = [sys.executable, "-m", "mt5copier", "--desktop", "--profile", name]
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: detached means *no console at all*, and
        # then every console-subsystem child the new copier spawns (each engine worker is
        # python.exe in a dev run) gets a fresh VISIBLE console from Windows. No-window
        # gives the child a real but invisible console its workers inherit. Outliving this
        # process needs no flag either way.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        try:
            subprocess.Popen(  # noqa: S603 -- our own executable, a vetted profile name
                cmd,
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return {"ok": False, "message": f"could not open {name!r}: {exc}"}
        log.info("profile %r opened in its own window", name)
        return {"ok": True, "message": f"{name!r} is opening in its own window…"}

    def profile_create(self, name: str) -> dict[str, Any]:
        name = str(name).strip()
        try:
            profiles_mod.create(name)
        except profiles_mod.ProfileError as exc:
            return {"ok": False, "message": str(exc)}
        log.info("profile %r created", name)
        return {"ok": True, "message": f"{name!r} made — open it to set it up"}

    def profile_rename(self, old: str, new: str) -> dict[str, Any]:
        old, new = str(old).strip(), str(new).strip()
        try:
            profiles_mod.rename(old, new)
        except profiles_mod.ProfileError as exc:
            return {"ok": False, "message": str(exc)}
        log.info("profile %r renamed to %r", old, new)
        return {"ok": True, "message": f"{old!r} is now {new!r}"}

    def profile_delete(self, name: str) -> dict[str, Any]:
        """Delete a profile and every record it keeps. The page confirms first; the model
        refuses the default and anything running, this window included."""
        name = str(name).strip()
        try:
            profiles_mod.delete(name)
        except profiles_mod.ProfileError as exc:
            return {"ok": False, "message": str(exc)}
        log.info("profile %r deleted", name)
        return {"ok": True, "message": f"deleted {name!r} and every record it kept"}

    # --- the guards page --------------------------------------------------------------------

    @staticmethod
    def _account_of_view(view: TerminalView) -> str | None:
        """The account logged into this terminal, as the engine resolves it: the latest
        snapshot's, else the one it was last known to be logged into."""
        snap = view.snapshot
        if snap is not None and snap.login is not None:
            return account_key(snap.login, snap.server)
        return account_key(view.cfg.last_login, view.cfg.last_server)

    def _guarded_view(self, tid: str) -> TerminalView | None:
        for view in self._engine.views():
            if view.cfg.terminal_id == tid:
                return view
        return None

    def _save_guards(self, key: str, guards: Any) -> str | None:
        """One assignment into the shared doc, then the file; the engine reads the doc live.
        Returns why the write failed, or None -- the change then holds until the app closes,
        which is said, exactly as the TUI says it."""
        self._guards_doc.set_account(key, guards)
        try:
            guards_model.save(self._guards_doc, guards_path())
        except OSError as exc:
            log.exception("guards save failed")
            return f"could not save the guards: {exc}; the change holds until the copier closes"
        return None

    @staticmethod
    def _when_text(ts: float, now: float) -> str:
        """``13:35`` today, ``Oct 10 13:35`` before -- the TUI guards page's wording."""
        here, today = time.localtime(ts), time.localtime(now)
        if here[:3] == today[:3]:
            return time.strftime("%H:%M", here)
        return time.strftime("%b %d %H:%M", here)

    @staticmethod
    def _limit_text(s: RuleStatus, currency: str) -> str:
        if s.limit is not None:
            out = money(s.limit, currency)
            if s.type == "total_margin":
                out += " in all"
            elif s.per_trade:
                out += " a trade"
            return out
        if s.type == "stop_required":
            return "a stop on every trade"
        if s.level is Level.UNKNOWN and s.enabled and not s.basis:
            return s.detail or "unknown"
        return "not known yet" if s.basis and s.enabled else ""

    @staticmethod
    def _used_cell(s: RuleStatus) -> dict[str, Any]:
        """The USED column, pre-decided: the page only draws what this says."""
        if s.latched is not None and s.enabled:
            return {
                "kind": "latched",
                "note": "held until the reset" if s.latched == "reset" else "held until cleared",
            }
        if not s.enabled or s.level is Level.OFF:
            return {"kind": "off"}
        if s.fraction is not None and s.used is not None:
            word = "in use" if s.type == "total_margin" else "used"
            return {
                "kind": "bar",
                "fraction": s.fraction,
                "holdsBack": s.holds_back,
                "moneyLine": f"{money(s.used)} {word}",
            }
        if s.type == "margin_per_trade":
            return {"kind": "text", "text": "checked as each trade is sent", "tone": "muted"}
        if s.level is Level.UNKNOWN:
            return {"kind": "text", "text": s.detail or "unknown", "tone": "yellow"}
        tone = {Level.OK: "blue", Level.NEAR: "yellow", Level.BREACHED: "red"}.get(s.level, "muted")
        return {"kind": "text", "text": s.detail, "tone": tone}

    def _rule_row(
        self, s: RuleStatus, currency: str, last: GuardAction | None, now: float
    ) -> dict[str, Any]:
        level_tone = {
            Level.OFF: "muted",
            Level.UNKNOWN: "muted",
            Level.OK: "blue",
            Level.NEAR: "yellow",
            Level.BREACHED: "red",
        }[s.level]
        last_out = None
        if last is not None:
            detail = last.detail.split(": ")[0]
            if len(detail) > 40:
                detail = last.detail[:39] + "…"
            last_out = {
                "when": self._when_text(last.ts, now),
                "action": last.action,
                "ticket": f"#{last.master_ticket}" if last.master_ticket is not None else "",
                "detail": detail,
            }
        return {
            "id": s.rule_id,
            "type": s.type,
            "title": s.title,
            "enabled": s.enabled,
            "tone": level_tone,
            "latched": s.latched,
            "limitText": self._limit_text(s, currency),
            "basis": s.basis,
            "used": self._used_cell(s),
            "onBreach": s.on_breach,
            "actAt": s.act_at,
            "last": last_out,
        }

    def guards_state(self, tid: str) -> dict[str, Any]:
        """Everything the Guards page redraws from: the account chips, the chosen account's
        report row by row (the TUI guards table's own cell logic), and the recent actions."""
        self._refresh_shared()
        engine = self._engine
        cfg = self._cfg
        now = time.time()
        tid = str(tid or "")
        chips: list[dict[str, Any]] = []
        chosen: TerminalView | None = None
        # Any account the copier knows can be guarded, not only a slave's: rules belong to
        # the account, and setting them before the terminal is assigned protects it from its
        # first copied trade. Slaves first -- they are the enforced ones -- and one chip per
        # account, under the slave's name where an account is open in two terminals.
        views = engine.views()
        seen_keys: set[str] = set()
        for want_slave in (True, False):
            for view in views:
                if (view.cfg.role is Role.SLAVE) is not want_slave:
                    continue
                key = self._account_of_view(view)
                if key is None:
                    # A slave with no known account still gets its chip, so the page can say
                    # "connect it once"; an unknown non-slave has nothing to key rules to.
                    if not want_slave:
                        continue
                elif key in seen_keys:
                    continue
                else:
                    seen_keys.add(key)
                report = engine.guard_report(view.cfg.terminal_id)
                worst = report.worst
                snap = view.snapshot
                server = snap.server if snap is not None and snap.server else view.cfg.last_server
                chips.append(
                    {
                        "tid": view.cfg.terminal_id,
                        "name": cfg.display_name(view.cfg.terminal_id),
                        "login": view.cfg.last_login,
                        "server": server,
                        "slave": want_slave,
                        "rules": len(report.rules),
                        "holding": bool(report.holding_back),
                        "worst": str(worst.level) if worst is not None else "",
                    }
                )
                if view.cfg.terminal_id == tid:
                    chosen = view
        if chosen is None and chips:
            tid = chips[0]["tid"]
            chosen = self._guarded_view(tid)
        if chosen is None:
            return {"accounts": [], "selected": None}

        report = engine.guard_report(tid)
        key = self._account_of_view(chosen)
        guards = self._guards_doc.for_account(key)
        actions = engine.guard_actions(tid)
        last_by_rule: dict[str, GuardAction] = {}
        for a in actions:
            if a.rule_id not in last_by_rule:
                last_by_rule[a.rule_id] = a
        rows = [
            self._rule_row(s, report.currency, last_by_rule.get(s.rule_id), now)
            for s in report.rules
        ]
        acted = [
            {
                "when": self._when_text(a.ts, now),
                "rule": a.rule,
                "action": a.action,
                "trade": f"#{a.master_ticket} {a.symbol} {a.label}".strip()
                if a.master_ticket is not None
                else "",
                "detail": a.detail,
            }
            for a in actions[:30]
        ]
        others = []
        offered: set[str] = set()
        for view in engine.views():
            vkey = account_key(view.cfg.last_login, view.cfg.last_server)
            if view.cfg.terminal_id == tid or vkey is None or vkey == key or vkey in offered:
                continue
            held = self._guards_doc.for_account(vkey).rules
            if held:
                offered.add(vkey)
                others.append(
                    {
                        "key": vkey,
                        "name": cfg.display_name(view.cfg.terminal_id),
                        "rules": len(held),
                    }
                )
        return {
            "accounts": chips,
            "selected": tid,
            "hasAccount": key is not None,
            "isSlave": chosen.cfg.role is Role.SLAVE,
            "currency": report.currency,
            "enabled": report.enabled,
            "initialBalance": guards.initial_balance,
            "rules": rows,
            "actions": acted,
            "ruleTypes": [
                {"type": t, "title": r.title, "blurb": r.blurb, "perTrade": r.per_trade()}
                for t, r in REGISTRY.items()
            ],
            "templates": [
                {"name": name, "rules": len(rules)}
                for name, rules in sorted(self._guards_doc.templates.items())
            ],
            "copyFrom": others,
        }

    def _account_and_guards(self, tid: str) -> tuple[str, Any] | str:
        """The selected terminal's account key and its guards, or why not."""
        view = self._guarded_view(str(tid))
        if view is None:
            return "that terminal is gone"
        key = self._account_of_view(view)
        if key is None:
            return "this terminal's account is not known yet: connect it once"
        return key, self._guards_doc.for_account(key)

    def guard_form(self, tid: str, rule_type: str, rule_id: str | None) -> dict[str, Any]:
        """The form for one rule, built from the parameters the rule declares -- the same
        declaration the TUI's RuleForm builds from, so a new rule needs nothing here."""
        rule_type = str(rule_type)
        kind = REGISTRY.get(rule_type)
        if kind is None:
            return {"ok": False, "message": f"{rule_type!r} is not a rule this version knows"}
        picked = self._account_and_guards(str(tid))
        if isinstance(picked, str):
            return {"ok": False, "message": picked}
        _, guards = picked
        rule = next((r for r in guards.rules if r.id == rule_id), None)
        if rule is None:
            rule = GuardRule(type=rule_type)
        values, _problems = rule.resolved()
        fields = []
        for p in kind.params:
            value = values.get(p.name, p.default)
            fields.append(
                {
                    "name": p.name,
                    "label": p.label,
                    "kind": str(p.kind),
                    "value": f"{value:g}" if isinstance(value, float) else str(value),
                    "choices": [[v, w] for v, w in p.choices],
                    "after": p.after,
                    "hint": ZONE_HINT if p.kind is ParamKind.ZONE else "",
                    "onlyIf": list(p.only_if) if p.only_if is not None else None,
                }
            )
        return {
            "ok": True,
            "title": kind.title,
            "blurb": kind.blurb,
            "enabled": rule.enabled,
            "fields": fields,
        }

    def _checked_params(
        self, rule_type: str, given: dict[str, Any]
    ) -> tuple[dict[str, ParamValue], dict[str, str]]:
        kind = REGISTRY[rule_type]
        values: dict[str, ParamValue] = {}
        errors: dict[str, str] = {}
        for p in kind.params:
            raw = str(given.get(p.name, "")).replace(",", "").strip()
            value, problem = p.check(raw)
            if problem is not None or value is None:
                errors[p.name] = problem or f"{p.label} is missing"
            else:
                values[p.name] = value
        return values, errors

    def guard_check(self, tid: str, rule_type: str, params: dict[str, Any]) -> dict[str, Any]:
        """Live validation for the form: the problems per field, and the sentence the rule
        reads as once every value checks."""
        rule_type = str(rule_type)
        if rule_type not in REGISTRY:
            return {"errors": {}, "sentence": ""}
        values, errors = self._checked_params(rule_type, params or {})
        sentence = ""
        if not errors:
            currency = self._engine.guard_report(str(tid)).currency
            sentence = REGISTRY[rule_type].sentence(values, currency)
        return {"errors": errors, "sentence": sentence}

    def guard_save_rule(
        self,
        tid: str,
        rule_type: str,
        rule_id: str | None,
        params: dict[str, Any],
        enabled: bool,
    ) -> dict[str, Any]:
        rule_type = str(rule_type)
        if rule_type not in REGISTRY:
            return {"ok": False, "message": f"{rule_type!r} is not a rule this version knows"}
        picked = self._account_and_guards(str(tid))
        if isinstance(picked, str):
            return {"ok": False, "message": picked}
        key, guards = picked
        values, errors = self._checked_params(rule_type, params or {})
        if errors:
            return {"ok": False, "errors": errors, "message": "correct the marked values"}
        title = REGISTRY[rule_type].title
        old = next((r for r in guards.rules if r.id == rule_id), None)
        if old is None:
            rules = (*guards.rules, GuardRule(type=rule_type, params=values, enabled=bool(enabled)))
        else:
            new = old.model_copy(update={"params": values, "enabled": bool(enabled)})
            rules = tuple(new if r.id == old.id else r for r in guards.rules)
        why = self._save_guards(key, guards.model_copy(update={"rules": rules}))
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"{title}: saved"}

    def guard_remove_rule(self, tid: str, rule_id: str) -> dict[str, Any]:
        picked = self._account_and_guards(str(tid))
        if isinstance(picked, str):
            return {"ok": False, "message": picked}
        key, guards = picked
        rule = next((r for r in guards.rules if r.id == rule_id), None)
        if rule is None:
            return {"ok": False, "message": "that rule is already gone"}
        title = REGISTRY[rule.type].title if rule.type in REGISTRY else rule.type
        rules = tuple(r for r in guards.rules if r.id != rule.id)
        why = self._save_guards(key, guards.model_copy(update={"rules": rules}))
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"{title}: removed"}

    def guard_toggle_rule(self, tid: str, rule_id: str) -> dict[str, Any]:
        picked = self._account_and_guards(str(tid))
        if isinstance(picked, str):
            return {"ok": False, "message": picked}
        key, guards = picked
        rule = next((r for r in guards.rules if r.id == rule_id), None)
        if rule is None:
            return {"ok": False, "message": "that rule is already gone"}
        flipped = rule.model_copy(update={"enabled": not rule.enabled})
        rules = tuple(flipped if r.id == rule.id else r for r in guards.rules)
        title = REGISTRY[rule.type].title if rule.type in REGISTRY else rule.type
        why = self._save_guards(key, guards.model_copy(update={"rules": rules}))
        if why is not None:
            return {"ok": False, "message": why}
        if flipped.enabled:
            return {"ok": True, "message": f"{title} is back on; it is enforced again"}
        return {
            "ok": True,
            "message": f"{title} is off; it will not check or act until switched back on",
        }

    def guard_toggle_all(self, tid: str) -> dict[str, Any]:
        picked = self._account_and_guards(str(tid))
        if isinstance(picked, str):
            return {"ok": False, "message": picked}
        key, guards = picked
        if not guards.rules:
            return {"ok": False, "message": "no rules to switch"}
        on_now = not guards.enabled
        why = self._save_guards(key, guards.model_copy(update={"enabled": on_now}))
        if why is not None:
            return {"ok": False, "message": why}
        if on_now:
            return {"ok": True, "message": "all guards on: every rule is enforced again"}
        return {
            "ok": True,
            "message": "all guards off: nothing is checked on this account until switched back on",
        }

    def guard_set_initial(self, tid: str, text: str) -> dict[str, Any]:
        picked = self._account_and_guards(str(tid))
        if isinstance(picked, str):
            return {"ok": False, "message": picked}
        key, guards = picked
        text = str(text or "").replace(",", "").replace(" ", "").strip()
        if not text:
            why = self._save_guards(key, guards.model_copy(update={"initial_balance": None}))
            return (
                {"ok": False, "message": why}
                if why
                else {"ok": True, "message": "initial balance cleared"}
            )
        try:
            value = float(text)
        except ValueError:
            return {"ok": False, "message": "the initial balance did not read as an amount"}
        if value <= 0:
            return {"ok": False, "message": "the initial balance must be more than 0"}
        why = self._save_guards(key, guards.model_copy(update={"initial_balance": value}))
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"initial balance {money(value)}"}

    def guard_clear_latch(self, tid: str, rule_id: str) -> dict[str, Any]:
        if self._engine.clear_guard_latch(str(tid), str(rule_id)):
            return {
                "ok": True,
                "message": "Breach cleared; new trades go through again once the rule is back under the point it acts at.",
            }
        return {"ok": False, "message": "that rule is not holding a breach"}

    def guard_save_template(self, tid: str, name: str) -> dict[str, Any]:
        picked = self._account_and_guards(str(tid))
        if isinstance(picked, str):
            return {"ok": False, "message": picked}
        _, guards = picked
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "message": "give the template a name"}
        if not guards.rules:
            return {"ok": False, "message": "no rules to save"}
        self._guards_doc.templates[name] = guards.rules
        try:
            guards_model.save(self._guards_doc, guards_path())
        except OSError as exc:
            log.exception("guards save failed")
            return {"ok": False, "message": f"could not save the guards: {exc}"}
        return {"ok": True, "message": f"template {name!r} saved"}

    def guard_apply_rules(self, tid: str, source: dict[str, str]) -> dict[str, Any]:
        """Replace this account's rules with a template's or another account's -- each under
        a new id, so nothing one account recorded is mistaken for the other's. The page asks
        before calling this when rules would be replaced."""
        picked = self._account_and_guards(str(tid))
        if isinstance(picked, str):
            return {"ok": False, "message": picked}
        key, guards = picked
        source = source or {}
        if "template" in source:
            held = self._guards_doc.templates.get(str(source["template"]))
            said = f"template {source['template']}"
        else:
            held = self._guards_doc.for_account(str(source.get("account", ""))).rules
            said = str(source.get("name") or "the other account")
        if not held:
            return {"ok": False, "message": "nothing to apply from there"}
        fresh = tuple(
            GuardRule(type=r.type, enabled=r.enabled, params=dict(r.params)) for r in held
        )
        why = self._save_guards(key, guards.model_copy(update={"rules": fresh}))
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"rules from {said}"}

    # --- the news page ----------------------------------------------------------------------
    # The calendar as the feed says it, and against each release what the news rules will
    # do -- computed from the same parameters the guard acts with, so the page never
    # promises anything the engine would not keep.

    def news_state(self) -> dict[str, Any]:
        """Everything the News page redraws from: the week's releases from today onward,
        each with the guard's answer to it, the filter fixings, and the next guarded one."""
        calendar = self._engine.news_calendar()
        now = time.time()
        cal = calendar.current()
        fetched = calendar.fetched_at()
        rules = self._news_rules()
        today = time.localtime(now)
        day_start = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))

        events: list[dict[str, Any]] = []
        currencies: set[str] = set()
        nxt: dict[str, Any] | None = None
        for e in cal.events or ():
            if e.when < day_start:
                continue
            counting = [r for r in rules if _news_counts(r["params"], e)]
            currencies.add(e.currency)
            events.append(
                {
                    "ts": e.when,
                    "day": self._news_day(e.when, now),
                    "time": "all day" if e.impact == "Holiday" else self._clock(e.when),
                    "ccy": e.currency,
                    "impact": e.impact,
                    "title": e.title,
                    "forecast": e.forecast,
                    "previous": e.previous,
                    "past": e.when < now,
                    "guarded": bool(counting),
                    "guard": self._news_cell(e, counting, rules),
                }
            )
            if nxt is None and e.when > now and counting:
                nxt = {
                    "ccy": e.currency,
                    "title": e.title,
                    "impact": e.impact,
                    "clock": self._clock(e.when),
                    "inWords": self._in_words(e.when - now),
                    "guard": self._news_banner(e, counting),
                }
        return {
            "known": cal.events is not None,
            "said": cal.said,
            "scanned": self._clock(fetched) if fetched else "",
            "ruled": bool(rules),
            "events": events,
            "currencies": sorted(currencies),
            "myCurrencies": self._my_currencies(),
            "next": nxt,
        }

    def news_rescan(self) -> dict[str, Any]:
        """Fetch the feed now, instead of on the hour. Synchronous: the page's button says
        it is scanning while this call runs on its own bridge thread."""
        calendar = self._engine.news_calendar()
        if calendar.refresh():
            cal = calendar.current()
            return {
                "ok": True,
                "message": f"calendar scanned: {len(cal.events or ())} releases this week",
            }
        cal = calendar.current()
        why = cal.said or "the feed could not be read"
        return {"ok": False, "message": f"{why}; the last copy stands"}

    def _news_rules(self) -> list[dict[str, Any]]:
        """Each enforced news rule: the account's display name and its resolved parameters.
        Enforced means what the guard book means -- a slave's account, the set switched on,
        the rule switched on; one entry per account."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for view in self._engine.views():
            if view.cfg.role is not Role.SLAVE:
                continue
            key = self._account_of_view(view)
            if key is None or key in seen:
                continue
            seen.add(key)
            guards = self._guards_doc.for_account(key)
            if not guards.enabled:
                continue
            for r in guards.rules:
                if r.type != "news" or not r.enabled:
                    continue
                params, _problems = r.resolved()
                out.append({"name": self._cfg.display_name(view.cfg.terminal_id), "params": params})
        return out

    def _news_cell(
        self, e: NewsEvent, counting: list[dict[str, Any]], rules: list[dict[str, Any]]
    ) -> str:
        """The GUARD column: what will be done about this release, or why nothing will.
        Identical rules on several accounts read as one line; differing ones are named."""
        if not counting:
            if rules and e.impact == MEDIUM and all(r["params"]["impact"] == "high" for r in rules):
                return "rule counts high impact only"
            return ""
        texts: dict[str, list[str]] = {}
        for r in counting:
            texts.setdefault(_news_said(r["params"], e), []).append(r["name"])
        if len(texts) == 1:
            return next(iter(texts))
        return " · ".join(f"{said} on {', '.join(names)}" for said, names in texts.items())

    def _news_banner(self, e: NewsEvent, counting: list[dict[str, Any]]) -> str:
        """The banner's sentence: the cell's answer plus who it protects."""
        names = sorted({r["name"] for r in counting})
        return f"{self._news_cell(e, counting, counting)} · guards {', '.join(names)}"

    def _my_currencies(self) -> list[str] | None:
        """The currencies the open book touches, for the page's narrowing filter. None when
        a symbol's name cannot be read -- any news moves it, so the filter cannot narrow."""
        engine = self._engine
        symbols = {p.symbol for p in engine.direct_positions()}
        symbols |= {o.symbol for o in engine.direct_orders()}
        for view in engine.views():
            snap = view.snapshot
            if snap is not None and snap.open_positions:
                symbols |= {p.symbol for p in snap.open_positions}
        out: set[str] = set()
        for s in symbols:
            ccys = symbol_currencies(s)
            if ccys is None:
                return None
            out |= ccys
        return sorted(out)

    @staticmethod
    def _clock(ts: float) -> str:
        return time.strftime("%H:%M", time.localtime(ts))

    @staticmethod
    def _news_day(ts: float, now: float) -> str:
        """The section a release files under: TODAY, TOMORROW, else the weekday."""
        here = time.localtime(ts)
        today = time.localtime(now)
        tomorrow = time.localtime(now + 86400)
        if here[:3] == today[:3]:
            return "TODAY"
        if here[:3] == tomorrow[:3]:
            return "TOMORROW"
        return time.strftime("%A", here).upper()

    @staticmethod
    def _in_words(seconds: float) -> str:
        s = max(0, int(seconds))
        if s < 60:
            return "now"
        if s < 3600:
            return f"in {s // 60} min"
        if s < 86400:
            return f"in {s // 3600} h {(s % 3600) // 60} min"
        return f"in {s // 86400} d {(s % 86400) // 3600} h"

    # --- the history page ------------------------------------------------------------------
    # What became of each copied trade on each slave, read back from the on-disk history.
    # The store keeps the facts and tui/history.py set the wording; the bridge only narrows
    # to what the filter asks and hands the page ready-worded rows.

    def history_state(self, tid: str, symbol: str, failed: bool, query: str) -> dict[str, Any]:
        """Everything the History page redraws from: the copied trades that pass the
        filter, the most recently copied first, each with every step taken with it --
        and the filter's own choices (the slaves, the symbols seen), read from everything
        kept so narrowing never hides where a choice would lead."""
        cfg = self._cfg
        now = time.time()
        slaves = [
            {"tid": t.terminal_id, "name": cfg.display_name(t.terminal_id)} for t in cfg.slaves
        ]
        tid = str(tid)
        wanted = [str(s["tid"]) for s in slaves] if tid in ("", "all") else [tid]
        kept: list[tuple[Trade, str]] = []
        for one in wanted:
            name = cfg.display_name(one)
            kept.extend((trade, name) for trade in self._engine.trade_history(one))
        kept.sort(key=lambda it: it[0].first, reverse=True)
        symbols = sorted({t.symbol for t, _ in kept})
        q = str(query).strip().lower()
        matched = [
            (t, name)
            for t, name in kept
            if (symbol in ("", "all") or t.symbol == symbol)
            and (not failed or t.problems)
            and (not q or q in f"#{t.master_ticket} {t.symbol} {t.label} {name}".lower())
        ]
        return {
            "days": cfg.history_days,
            "slaves": slaves,
            "symbols": symbols,
            "total": len(kept),
            "matched": len(matched),
            "trades": [self._trade_card(t, name, now) for t, name in matched[:_HISTORY_SHOWN]],
        }

    def _trade_card(self, trade: Trade, slave: str, now: float) -> dict[str, Any]:
        entry = trade.entry
        return {
            "key": f"{trade.terminal_id}|{trade.master_ticket}",
            "slave": slave,
            "ticket": trade.master_ticket,
            "symbol": trade.symbol,
            "label": trade.label,
            "lots": entry.lots if entry is not None else None,
            "status": trade.status,
            "problems": trade.problems,
            "steps": [self._step_row(s, now) for s in trade.steps],
        }

    def _step_row(self, step: Step, now: float) -> dict[str, Any]:
        return {
            "when": self._step_when(step.ts, now),
            "action": step.action,
            "detail": step.slave,
            "master": step.master,
            "guard": step.guard,
            "tone": _step_tone(step),
        }

    @staticmethod
    def _step_when(ts: float, now: float) -> str:
        """``23:08:15`` today, ``04 Oct 23:08`` before it -- the TUI history page's clock."""
        here, today = time.localtime(ts), time.localtime(now)
        if here[:3] == today[:3]:
            return time.strftime("%H:%M:%S", here)
        return time.strftime("%d %b %H:%M", here)

    # --- the log dock --------------------------------------------------------------------------
    # The TUI log page's read, served to a dock that sits under every page: copier.log and
    # the day book merged (mt5copier/log_entries.py owns the parsing, the attribution and
    # the merge), narrowed in the bridge the way the history page narrows.

    def _log_entries(self) -> list[LogEntry]:
        """``copier.log`` and the day book, merged. The file is parsed again only when it
        has changed; the day book is cheap to read every time."""
        cfg = self._cfg
        path = log_dir() / "copier.log"
        try:
            stat = path.stat()
            seen: object = (stat.st_size, stat.st_mtime)
        except OSError:
            seen = None
        since = self._engine.day_book_since
        stamp = (seen, since, tuple((tid, t.label) for tid, t in cfg.terminals.items()))
        if stamp != self._log_cache[0]:
            names = terminal_names(
                {tid: (t.label, cfg.display_name(tid)) for tid, t in cfg.terminals.items()}
            )
            text = read_log_tail(path, max_lines=_LOG_LINES)
            self._log_cache = (stamp, file_entries(text, names, covered_from=since))
        master = cfg.master
        events = event_entries(self._engine.day_book(), master.terminal_id if master else None)
        return merge(self._log_cache[1], events)

    def _log_who(self, entry: LogEntry, numbers: dict[str, str]) -> str:
        """The Who column, as the day book names them: "Master (name)", "Slave 2 (name)"."""
        if entry.terminal_id is None:
            return "copier"
        number = numbers.get(entry.terminal_id)
        if number == "A":
            role = "Master"
        elif number:
            role = f"Slave {number}"
        else:
            role = "Terminal"
        return f"{role} ({self._cfg.display_name(entry.terminal_id)[:24]})"

    def log_state(self, tid: str, problems: bool, query: str) -> dict[str, Any]:
        """Everything the log dock redraws from. Narrowing is in the bridge, as the history
        page's is: the search reaches what a line shows -- its level and terminal as well as
        its text -- and the problems count stays honest under the problems-only tick."""
        cfg = self._cfg
        tid, query = str(tid), str(query)
        numbers = {t.terminal_id: str(n) for n, t in enumerate(cfg.slaves, start=1)}
        master = cfg.master
        if master is not None:
            numbers[master.terminal_id] = "A"

        def who(entry: LogEntry) -> str:
            return self._log_who(entry, numbers)

        scoped = select(self._log_entries(), problems_only=False, terminal_id=tid or None)
        scoped = search(scoped, search_words(query), who)
        shown = select(scoped, problems_only=bool(problems), terminal_id=None)
        now = time.time()
        return {
            "rows": [
                {
                    "when": self._step_when(e.ts, now),
                    "who": who(e),
                    "tid": e.terminal_id,
                    "text": e.text,
                    "tone": e.tone.value,
                }
                for e in shown[-_LOG_SHOWN:]
            ],
            "problems": sum(1 for e in scoped if e.level.problem),
            "matched": len(shown),
            "terminals": [{"tid": t, "name": cfg.display_name(t)} for t in cfg.terminals],
        }

    # --- the setups page ---------------------------------------------------------------------
    # A setup is who copies from whom: the master, its slaves, the account each is logged
    # into, every one's settings, and whether copying was on and live. What one holds is
    # mt5copier/setups.py's and loading one is the engine's; the bridge words the page
    # (tui/setups.py's own sentences) and runs the logins a load needs.

    def _save_setups(self) -> str | None:
        try:
            setups_mod.save(self._setups, setups_path())
        except OSError as exc:
            log.exception("setups save failed")
            return f"could not save the setups: {exc}; the change holds until the copier closes"
        return None

    def _setup_on_account(self, setup: Setup, tid: str) -> str:
        """A terminal's name, and the account the setup has it on if it keeps one."""
        login = setup.accounts.get(tid)
        return setups_mod.name_of(self._cfg, tid) + (f" · {login}" if login is not None else "")

    def _setup_slaves_text(self, setup: Setup) -> str:
        if not setup.slaves:
            return "none"
        return ", ".join(self._setup_on_account(setup, tid) for tid in setup.slaves)

    def _setup_describe(self, setup: Setup) -> str:
        """The roles a setup puts in place, as the change a confirmation names."""
        n = len(setup.slaves)
        said = f"master {self._setup_on_account(setup, setup.master)}, "
        said += f"{n} slave{'' if n == 1 else 's'}"
        said += f" ({self._setup_slaves_text(setup)})" if n else ""
        said += ", copying " + ("on" if setup.copying else "off")
        if setup.dry_run is not None:
            said += " (dry run)" if setup.dry_run else " (LIVE)"
        if setup.settings:
            said += ", settings as saved"
        return said

    @staticmethod
    def _saved_when(ts: float, now: float) -> str:
        """``today 13:35``, or the full date once it is not -- the TUI setups page's clock."""
        here, today = time.localtime(ts), time.localtime(now)
        if here[:3] == today[:3]:
            return time.strftime("today %H:%M", here)
        return time.strftime("%d %b %Y %H:%M", here)

    def _account_called(self, key: str) -> str:
        called = self._cfg.account_names.get(key)
        return f"{called} ({key})" if called else key

    def setups_state(self) -> dict[str, Any]:
        """Everything the Setups page redraws from: the roles in place now against the
        saved setups, and each setup's row with anything stopping it loading here."""
        cfg = self._cfg
        doc = self._setups
        now = time.time()
        logins = self._engine.logins()
        live = setups_mod.capture(cfg, logins)
        current = setups_mod.current_name(doc, cfg, logins)
        changed: list[str] = []
        if current is not None:
            changed = [
                setups_mod.name_of(cfg, t)
                for t in setups_mod.changed_settings(doc.setups[current], cfg)
            ]
        rows = [
            {
                "name": name,
                "master": self._setup_on_account(s, s.master),
                "slaves": self._setup_slaves_text(s),
                "copying": s.copying,
                "dryRun": s.dry_run,
                "saved": self._saved_when(s.saved_at, now),
                "current": name == current,
                "problems": setups_mod.problems(s, cfg, logins),
            }
            for name, s in sorted(doc.setups.items())
        ]
        return {
            "direct": cfg.mode is AppMode.TRADING,
            "now": self._setup_describe(live) if live is not None else None,
            "current": current,
            "changed": changed,
            "loading": self._setup_busy,
            "setups": rows,
        }

    def _captured_or_why(self) -> Setup | str:
        """The roles in place now as a setup to save, or why there is nothing to save."""
        if self._cfg.mode is AppMode.TRADING:
            return "this profile trades directly; setups are a copy-mode thing"
        live = setups_mod.capture(self._cfg, self._engine.logins())
        if live is None:
            return "no master is assigned: drag one into the MASTER zone before saving a setup"
        return live

    def setup_save(self, name: str) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "message": "give the setup a name"}
        if name in self._setups.setups:
            return {
                "ok": False,
                "message": f"{name} is already saved: pick it and press Update with current",
            }
        live = self._captured_or_why()
        if isinstance(live, str):
            return {"ok": False, "message": live}
        self._setups.setups[name] = live
        why = self._save_setups()
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"setup {name} saved"}

    def setup_update(self, name: str) -> dict[str, Any]:
        """Save an existing setup again as the roles in place now. The page asks first."""
        name = str(name)
        if name not in self._setups.setups:
            return {"ok": False, "message": "that setup is gone"}
        live = self._captured_or_why()
        if isinstance(live, str):
            return {"ok": False, "message": live}
        self._setups.setups[name] = live
        why = self._save_setups()
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"setup {name} updated"}

    def setup_rename(self, old: str, new: str) -> dict[str, Any]:
        old, new = str(old), str(new or "").strip()
        setups = self._setups.setups
        if old not in setups:
            return {"ok": False, "message": "that setup is gone"}
        if not new:
            return {"ok": False, "message": "give the setup a name"}
        if new == old:
            return {"ok": True, "message": f"setup {old} keeps its name"}
        if new in setups:
            return {"ok": False, "message": f"a setup called {new} is already saved"}
        setups[new] = setups.pop(old)
        why = self._save_setups()
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"setup {old} renamed {new}"}

    def setup_delete(self, name: str) -> dict[str, Any]:
        """Forget a saved setup. The page asks first; the roles in place do not change."""
        name = str(name)
        if self._setups.setups.pop(name, None) is None:
            return {"ok": False, "message": "that setup is already gone"}
        why = self._save_setups()
        if why is not None:
            return {"ok": False, "message": why}
        return {"ok": True, "message": f"setup {name} deleted"}

    def setup_load_ask(self, name: str) -> dict[str, Any]:
        """What loading this setup would do, for the page to ask with: the change as one
        sentence, the copies it strands, and whether it turns the dry run live. Nothing
        happens here; :meth:`setup_load` commits, and the engine re-checks everything."""
        name = str(name)
        setup = self._setups.setups.get(name)
        if setup is None:
            return {"ok": False, "message": "that setup is gone"}
        if self._setup_busy:
            return {"ok": False, "message": "a setup is already loading; wait for it to finish"}
        cfg = self._cfg
        if cfg.mode is AppMode.TRADING:
            # problems() says this too, but with the TUI's key in the sentence.
            return {
                "ok": False,
                "message": "this profile trades directly; setups are a copy-mode thing",
            }
        logins = self._engine.logins()
        found = setups_mod.problems(setup, cfg, logins)
        if found:
            return {"ok": False, "message": f"setup {name} cannot be loaded: {'; '.join(found)}"}
        if setups_mod.in_place(setup, cfg, logins):
            return {"ok": False, "already": True, "message": f"setup {name} is already in place"}
        # Unreadable reads as nothing to ask about; the engine reads again and refuses.
        stake = self._engine.setup_stake(setup) or {}
        changes = self._setup_describe(setup)
        held = setups_mod.held_back(setup, cfg)
        if held:
            changes += (
                "; live trading stays off on "
                + ", ".join(cfg.display_name(t) for t in held)
                + " (a setup never turns it on)"
            )
        switching: list[str] = []
        logs_in: list[str] = []
        for tid, login in setups_mod.switches(setup, logins).items():
            account = setups_mod.find_account(cfg, tid, login)
            if isinstance(account, str):  # problems() has checked; a race reads as a refusal
                return {"ok": False, "message": f"setup {name} cannot be loaded: {account}"}
            key = account_key(account.login, account.server) or ""
            switching.append(cfg.display_name(tid))
            logs_in.append(f"{cfg.display_name(tid)} logs in to {self._account_called(key)}")
        if logs_in:
            changes += "; " + ", ".join(logs_in)
        dry_after = cfg.dry_run if setup.dry_run is None else setup.dry_run
        return {
            "ok": True,
            "changes": changes,
            "stake": [
                {
                    "tid": slave_tid,
                    "name": cfg.display_name(slave_tid),
                    "copies": [
                        {
                            "masterTicket": c.master_ticket,
                            "slaveTicket": c.slave_ticket,
                            "symbol": c.symbol,
                            "volume": c.volume,
                            "resting": c.resting,
                        }
                        for c in held_copies
                    ],
                }
                for slave_tid, held_copies in stake.items()
            ],
            "goesLive": setups_mod.goes_live(setup, cfg),
            "copying": setup.copying,
            "live": setup.copying and not dry_after,
            "slaves": [cfg.display_name(t) for t in setup.slaves if t in cfg.terminals],
            "switching": switching,
        }

    def setup_load(
        self, name: str, agreed: dict[str, list[int]] | None, go_live: bool
    ) -> dict[str, Any]:
        """Commit the load the page asked about: roles, settings, copying and the mode --
        and first, any terminal the setup has on another account is logged into its saved
        one, one at a time, blocking this bridge thread; the page's promise narrates while
        it runs and the other polls keep arriving on their own threads.

        ``agreed`` is what the person was shown and said yes to, per slave the master
        tickets, exactly as the engine re-checks it. ``go_live`` only once LIVE has been
        typed; without it the engine refuses a setup that turns the dry run live."""
        with self._busy_lock:
            if self._setup_busy:
                return {
                    "ok": False,
                    "messages": [
                        {"text": "a setup is already loading; wait for it to finish", "tone": "bad"}
                    ],
                }
            self._setup_busy = True
        try:
            return self._setup_load(str(name), agreed, bool(go_live))
        finally:
            self._setup_busy = False

    def _setup_load(
        self, name: str, agreed: dict[str, list[int]] | None, go_live: bool
    ) -> dict[str, Any]:
        cfg = self._cfg

        def refused(text: str) -> dict[str, Any]:
            return {"ok": False, "messages": [{"text": text, "tone": "bad"}]}

        setup = self._setups.setups.get(name)
        if setup is None:
            return refused("that setup is gone")
        if cfg.mode is AppMode.TRADING:
            return refused("this profile trades directly; setups are a copy-mode thing")
        logins = self._engine.logins()
        found = setups_mod.problems(setup, cfg, logins)
        if found:
            return refused(f"setup {name} was not loaded: {'; '.join(found)}")
        accounts: dict[str, account_switch.Account] = {}
        for tid, login in setups_mod.switches(setup, logins).items():
            account = setups_mod.find_account(cfg, tid, login)
            if isinstance(account, str):
                return refused(f"setup {name} was not loaded: {account}")
            accounts[tid] = account
        orphan = (
            {str(t): [int(n) for n in tickets] for t, tickets in agreed.items()} if agreed else None
        )
        held = setups_mod.held_back(setup, cfg)
        refusal = self._engine.load_setup(
            setup, orphan=orphan, leave_out=list(accounts), go_live=go_live
        )
        if refusal is not None:
            return refused(f"setup {name} was not loaded: {refusal}")
        messages: list[dict[str, str]] = []
        why = self._persist()
        if why is not None:
            messages.append({"text": why, "tone": "bad"})
        if held:
            messages.append(
                {
                    "text": "live trading was allowed on "
                    + ", ".join(cfg.display_name(t) for t in held)
                    + " when the setup was saved; it stays off until you allow it from the"
                    " terminal's own menu",
                    "tone": "warn",
                }
            )
        failed: list[str] = []
        for tid, acct in accounts.items():
            result = self._log_in(tid, acct)
            key = account_key(acct.login, acct.server) or ""
            if result.ok:
                # Keep the account the login left the terminal on, as the one it was last on.
                tcfg = cfg.terminals[tid]
                cfg.replace_terminal(record_login(tcfg, result.login, result.server or acct.server))
                continue
            failed.append(tid)
            messages.append(
                {
                    "text": f"{cfg.display_name(tid)} was not logged in to "
                    f"{self._account_called(key)}: {result.said}. It is left unassigned",
                    "tone": "bad",
                }
            )
        if accounts:
            refusal = self._engine.load_setup(setup, leave_out=failed)
            why = self._persist()
            if why is not None:
                messages.append({"text": why, "tone": "bad"})
            if refusal is not None:
                messages.append(
                    {"text": f"setup {name} was not loaded in full: {refusal}", "tone": "bad"}
                )
                return {"ok": False, "messages": messages}
            if setup.master in failed:
                messages.append(
                    {
                        "text": f"setup {name} was not loaded: its master could not log in",
                        "tone": "bad",
                    }
                )
                return {"ok": False, "messages": messages}
        said = f"{name} (all but what could not log in)" if failed else name
        if not setup.copying:
            messages.append({"text": f"setup {said} loaded; copying is off", "tone": "ok"})
        elif cfg.dry_run:
            messages.append(
                {
                    "text": f"setup {said} loaded; detection on: dry run, nothing will be sent",
                    "tone": "ok",
                }
            )
        else:
            messages.append(
                {
                    "text": f"setup {said} loaded. COPYING LIVE: orders will be sent to every"
                    " connected slave",
                    "tone": "warn",
                }
            )
        orphaned = sum(len(t) for t in (orphan or {}).values())
        if orphaned:
            messages.append(
                {
                    "text": f"{orphaned} cop{'y' if orphaned == 1 else 'ies'} orphaned: they stay"
                    " open on their slaves and are no longer managed",
                    "tone": "warn",
                }
            )
        return {"ok": True, "messages": messages}

    def _log_in(self, tid: str, acct: account_switch.Account) -> account_switch.LoginResult:
        """Log one terminal in, blocking this bridge thread: each login starts its
        terminal, and one at a time they need not compete."""
        exe = self._cfg.terminals[tid].exe_path
        if exe is None:  # problems() has checked usable; a race reads as a failed login
            return account_switch.LoginResult(False, "it has no terminal executable")
        try:
            return account_switch.log_in(exe, acct, log_dir() / f"login-{tid[:8]}.log")
        except Exception as exc:
            log.exception("logging %s in to %s@%s failed", tid[:8], acct.login, acct.server)
            return account_switch.LoginResult(False, f"the login process failed: {exc!r}")

    # --- the settings page --------------------------------------------------------------------
    # The copier's own settings, the TUI g form: what a value means, its bounds, and the two
    # gates on the mode (copying stopped, LIVE typed) are all general_edit's. This translates.

    def _slave_names(self) -> list[str]:
        return [self._cfg.display_name(t.terminal_id) for t in self._cfg.slaves]

    def settings_state(self) -> dict[str, Any]:
        """What the form fills from, and the facts the notes above it word."""
        cfg = self._cfg
        return {
            "profile": active_profile(),
            "values": initial_general(cfg),
            "profileMode": cfg.mode.value,
            "copying": cfg.copying_enabled,
            "dryRun": cfg.dry_run,
        }

    @staticmethod
    def _general_raw(raw: dict[str, Any]) -> dict[str, str | bool]:
        """The page's fields as apply_general reads them: the mode a bool, the rest text.
        Only known names pass -- an extra key is a page bug, not a reason to refuse a save."""
        out: dict[str, str | bool] = {}
        for name in GENERAL:
            if name not in raw:
                continue
            value = raw[name]
            out[name] = value if isinstance(value, bool) else str(value)
        return out

    @staticmethod
    def _desk_errors(errors: dict[str, str]) -> dict[str, str]:
        """The model's messages with the TUI's keys reworded for this window: the mode gate
        names d, which here is the title bar's halt button."""
        say = {
            "copying is running: stop it with d first, then change the mode": (
                "copying is running: halt it (the title bar button) first, then change the mode"
            ),
        }
        return {name: say.get(text, text) for name, text in errors.items()}

    def settings_check(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Per-field errors and what a save would change, as the person types. Nothing is
        saved; the sentence keeps the Save button honest the way the guards form's READS-AS
        does."""
        outcome = apply_general(self._cfg, self._general_raw(raw))
        return {
            "errors": self._desk_errors(outcome.errors),
            "changes": describe_changes(outcome.changes, limit=160) if outcome.changes else "",
            "goesLive": outcome.confirm_live,
        }

    def settings_save(
        self, raw: dict[str, Any], profile_mode: str, typed: str | None = None
    ) -> dict[str, Any]:
        """Save the form, the TUI GeneralForm's own order: everything is checked first, the
        mode switch is ruled on by the engine, and only then is anything written -- a form
        half-applied is worse than one refused whole. ``typed`` is the LIVE confirmation;
        None when not asked yet, and an edit that goes live without it answers ``needLive``
        so the page can ask and send again."""
        cfg = self._cfg
        outcome = apply_general(cfg, self._general_raw(raw), typed=typed)
        if outcome.confirm_live and typed is None:
            return {"ok": False, "needLive": True, "slaves": self._slave_names()}
        if not outcome.ok:
            return {
                "ok": False,
                "errors": self._desk_errors(outcome.errors),
                "message": "Correct the marked values; nothing was changed.",
            }

        try:
            wanted = AppMode(profile_mode)
        except ValueError:
            return {"ok": False, "message": f"unknown profile mode {profile_mode!r}"}
        switched = False
        if wanted is not cfg.mode:
            refusal = self._engine.set_mode(wanted)
            if refusal is not None:
                return {"ok": False, "errors": {"profile_mode": refusal}, "message": refusal}
            switched = True

        if not outcome.changes and not switched:
            return {
                "ok": True,
                "messages": [{"text": "The copier's settings: no changes.", "tone": ""}],
            }

        messages: list[dict[str, str]] = []
        if outcome.changes:
            commit_general(cfg, outcome)
            log.warning("copier settings changed: %s", describe_changes(outcome.changes))
            messages.append(
                {"text": f"Saved: {describe_changes(outcome.changes, limit=160)}.", "tone": "ok"}
            )
        if switched:
            messages.append(
                {
                    "text": "This profile now trades directly: the Trading page places orders, "
                    "and every enabled slave copies them."
                    if wanted is AppMode.TRADING
                    else "This profile now copies a master terminal: drag one into the MASTER "
                    "zone on Terminals.",
                    "tone": "warn",
                }
            )
        changed = {c.name for c in outcome.changes}
        if DRY_RUN in changed and not cfg.dry_run:
            messages.append(
                {
                    "text": "LIVE: real orders are sent once copying resumes — the title "
                    "bar button.",
                    "tone": "warn",
                }
            )
        if changed & set(APPLIES_ON_CONNECT):
            messages.append(
                {
                    "text": "The check interval and retry schedule reach each terminal the "
                    "next time its connection starts.",
                    "tone": "",
                }
            )
        why = self._persist()
        if why is not None:
            messages.append({"text": why, "tone": "bad"})
        return {"ok": True, "messages": messages}

    # --- the terminal detail page -------------------------------------------------------------
    # One terminal's own page: its header facts and its settings form, the TUI s form for a
    # slave and the m form for the master. What a value means, its bounds, and that an edit
    # keeps open copies are settings_edit's and the engine's; this translates, and works the
    # example with the same gather the TUI form does.

    _MASTER_FIELDS = (
        "management",
        "breakeven_mode",
        "breakeven_trigger_r",
        "breakeven_book_percent",
        "symbol_prefix",
        "symbol_suffix",
    )
    _BROKERS_OWN = ("symbol_prefix", "symbol_suffix", "symbol_map")
    """Names that fit one broker's symbols, so copying another slave's settings leaves them."""

    def _view_of(self, tid: str) -> TerminalView | None:
        return next((v for v in self._engine.views() if v.cfg.terminal_id == tid), None)

    def _slave_gather(
        self, tid: str
    ) -> tuple[float | None, list[MasterTrade], dict[int, PriceValue], dict[int, str]]:
        """The slave's balance, the master's trades and this slave's price values -- the TUI
        form's _gather, read fresh on each check: the example is about the settings, worked
        on whatever is true now."""
        engine = self._engine
        cfg = self._cfg
        views = {v.cfg.terminal_id: v for v in engine.views()}
        view = views.get(tid)
        balance = view.snapshot.balance if view is not None and view.snapshot else None

        held: list[MasterTrade] = []
        master = cfg.master
        mview = views.get(master.terminal_id) if master is not None else None
        if master and mview and mview.snapshot and mview.snapshot.open_positions:
            for p in sorted(mview.snapshot.open_positions, key=lambda p: p.time, reverse=True):
                name = undecorate(p.symbol, master.symbol_prefix, master.symbol_suffix)
                held.append(
                    MasterTrade(
                        p.ticket, name, p.type is PositionType.BUY, p.price_open, p.sl, p.tp
                    )
                )
        events_ = engine.day_book()
        trades = _detail_trades(held, events_)
        symbols = {t.ticket: t.symbol for t in trades}
        for e in events_:
            symbols.setdefault(e.master_ticket, e.symbol)
        fills: dict[int, PriceValue] = {}
        for c in engine.open_copies(tid) or ():
            if c.price_value > 0:
                fills[c.master_ticket] = PriceValue(c.price_value, c.point)
        for r in reversed(engine.recent_results()):
            res = r.result
            if res.terminal_id == tid and res.ok and res.price_value:
                fills.setdefault(res.master_ticket, PriceValue(res.price_value, res.point or 0.0))
        return balance, trades, fills, symbols

    def _example_dict(self, tid: str, checked: TerminalConfig | None) -> dict[str, str]:
        if checked is None:
            return {
                "lead": "Correct the settings above to see what this slave would open.",
                "working": "",
                "result": "",
                "note": "",
            }
        balance, trades, fills, symbols = self._slave_gather(tid)
        trade, price = pick(trades, fills, symbols, checked.invert)
        ex = work(checked, balance, trade, price)
        return {"lead": ex.lead, "working": ex.working, "result": ex.result, "note": ex.note}

    def _master_symbols(self, tid: str) -> list[str]:
        """The master's held symbol names, for the decoration example."""
        view = self._view_of(tid)
        held = (view.snapshot.open_positions if view is not None and view.snapshot else None) or ()
        return list(dict.fromkeys(p.symbol for p in held))

    @staticmethod
    def _symbol_example(symbols: list[str], prefix: str, suffix: str) -> list[dict[str, str]]:
        """What the slaves will be given for a few of the master's names -- tui/master.py's
        example, as rows the page draws."""
        prefix, suffix = prefix.strip(), suffix.strip()
        if not symbols:
            symbols = [f"{prefix}EURUSD{suffix}"] if prefix or suffix else ["EURUSD"]
        return [{"symbol": s, "plain": undecorate(s, prefix, suffix)} for s in symbols[:4]]

    def _stake_line(self, tid: str) -> dict[str, str]:
        """The TUI form's footing: what a change does to the copies this slave holds."""
        held = self._engine.open_copies(tid)
        if held is None:
            return {
                "text": "Could not read what is open on this slave: you are asked before a "
                "change gives anything up.",
                "tone": "red",
            }
        if not held:
            return {
                "text": "Nothing is open on this slave, so a change gives nothing up.",
                "tone": "",
            }
        return {
            "text": f"{copies_word(len(held))} on this slave "
            f"{'stays' if len(held) == 1 else 'stay'} managed through a change, each as it "
            "was opened.",
            "tone": "",
        }

    def _account_kind(self, tcfg: TerminalConfig) -> str:
        """The header's account fact, the TUI form's own four states."""
        view = self._view_of(tcfg.terminal_id)
        snap = view.snapshot if view is not None else None
        if snap is None or snap.trade_mode is None:
            return "unread"
        if snap.trade_mode == ACCOUNT_TRADE_MODE_DEMO:
            return "demo"
        return "live-allowed" if tcfg.allow_live_trading else "live-held"

    def terminal_detail_state(self, tid: str) -> dict[str, Any]:
        """Everything the detail page redraws from, in one call per poll."""
        tid = str(tid)
        cfg = self._cfg
        tcfg = cfg.terminals.get(tid)
        if tcfg is None:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        view = self._view_of(tid)
        card = self._card(view, self._engine.holds()) if view is not None else None
        out: dict[str, Any] = {
            "ok": True,
            "tid": tid,
            "name": cfg.display_name(tid),
            "role": str(tcfg.role),
            "card": card,
            "dryRun": cfg.dry_run,
            "accountKind": self._account_kind(tcfg),
        }
        if tcfg.role is Role.SLAVE:
            values = initial_values(tcfg)
            # The one-line input's spelling; apply_edits parses commas and newlines alike.
            values["symbol_map"] = ", ".join(f"{k}={t}" for k, t in sorted(tcfg.symbol_map.items()))
            master = cfg.master
            out["values"] = values
            out["masterSays"] = master is not None and master.management is Management.MASTER
            out["stake"] = self._stake_line(tid)
            out["example"] = self._example_dict(tid, tcfg)
            out["copyFrom"] = [
                {"tid": c.terminal_id, "name": cfg.display_name(c.terminal_id)}
                for c in cfg.slaves
                if c.terminal_id != tid
            ]
        elif tcfg.role is Role.MASTER:
            out["values"] = {
                name: initial_text(tcfg, BY_NAME[name]) for name in self._MASTER_FIELDS
            }
            out["symbolExample"] = self._symbol_example(
                self._master_symbols(tid), tcfg.symbol_prefix, tcfg.symbol_suffix
            )
        return out

    def terminal_overview_state(self, tid: str) -> dict[str, Any]:
        """Everything the detail page's Overview tab redraws from, in one call per poll:
        the TUI enter page's three ruled columns, then the positions and orders tables.
        Display only, end to end -- nothing here feeds back into what is copied."""
        tid = str(tid)
        cfg = self._cfg
        tcfg = cfg.terminals.get(tid)
        if tcfg is None:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        engine = self._engine
        holds = engine.holds()
        views = engine.views()
        view = next((v for v in views if v.cfg.terminal_id == tid), None)
        snap = view.snapshot if view is not None else None
        stats = engine.terminal_stats(tid)
        ping = format_ping(snap.ping_last_us if snap is not None else None)
        out: dict[str, Any] = {
            "ok": True,
            "tid": tid,
            "name": cfg.display_name(tid),
            "role": str(tcfg.role),
            "card": self._card(view, holds) if view is not None else None,
            "dryRun": cfg.dry_run,
            "accountKind": self._account_kind(tcfg),
            "connected": format_duration(stats.connected_for_s)
            if stats.connected_for_s is not None
            else None,
            "ping": None if ping == "—" else ping,
            "leverage": snap.leverage if snap is not None else None,
            "account": _ov_account(snap),
        }

        slaves = [v for v in views if v.cfg.role is Role.SLAVE]
        if tcfg.role is Role.MASTER:
            out["copiedTo"] = {
                "who": "master manages"
                if tcfg.management is Management.MASTER
                else "slaves manage",
                "slaves": [
                    {
                        "name": cfg.display_name(v.cfg.terminal_id),
                        "card": self._card(v, holds),
                    }
                    for v in slaves
                ],
            }
        else:
            out["rule"] = _ov_rule_rows(
                tcfg,
                snap.currency if snap is not None else None,
                _ov_example_symbol(views),
                managed=cfg.manages(tcfg),
            )

        if tcfg.role is Role.SLAVE:
            out["copying"] = {
                "rows": _ov_copying_rows(stats),
                "latencies": [round(v, 1) for v in stats.latencies_ms],
            }
        else:
            out["connection"] = {
                "rows": _ov_connection_rows(stats, snap),
                "note": "The master is only read from, so there is no copying to time here."
                if tcfg.role is Role.MASTER
                else "Copying is measured on slaves only.",
            }

        out["positions"] = None
        out["orders"] = None
        if snap is not None:
            is_slave = tcfg.role is Role.SLAVE
            copies = engine.open_copies(tid) if is_slave else None
            if tcfg.role is Role.MASTER:
                rows = position_rows(
                    snap,
                    is_master=True,
                    held_by=held_by_counts(engine.open_copies(v.cfg.terminal_id) for v in slaves),
                    slave_count=len(slaves),
                )
            else:
                mview = next((v for v in views if v.cfg.role is Role.MASTER), None)
                if engine.direct_mode():
                    # Direct trading has no master terminal: the virtual book is the
                    # master side, or every healthy copy would read "closed on master".
                    master_positions: tuple[Position, ...] | None = engine.direct_positions()
                else:
                    master_positions = (
                        mview.snapshot.open_positions
                        if mview is not None and mview.snapshot is not None
                        else None
                    )
                rows = position_rows(
                    snap,
                    is_master=False,
                    copies=copies,
                    breakeven=breakeven_rule(tcfg),
                    master_positions=master_positions,
                    orphan_tickets=(o.slave_ticket for o in engine.orphans(tid)),
                )
            by_ticket = {
                c.slave_ticket: c
                for c in copies or ()
                if c.slave_ticket is not None and not c.resting
            }
            prows = None
            total = None
            if rows is not None:
                prows = [
                    _ov_position_row(r, by_ticket.get(r.ticket), tcfg, is_slave=is_slave)
                    for r in rows
                ]
                total = sum(r.profit for r in rows if r.profit is not None)
            out["positions"] = {
                "unreadable": rows is None,
                "rows": prows or [],
                "total": total,
                "exposure": _ov_exposure(snap.open_positions),
            }
            orders = snap.pending_orders
            if orders is None:
                if snap.is_live:
                    out["orders"] = {"unreadable": True, "rows": [], "anyStopLimit": False}
            else:
                resting = {
                    c.slave_ticket: c
                    for c in copies or ()
                    if c.resting and c.slave_ticket is not None
                }
                ordered = sorted(orders, key=lambda o: (o.symbol, o.ticket))
                now = time.time()
                out["orders"] = {
                    "unreadable": False,
                    "rows": [
                        _ov_order_row(o, resting.get(o.ticket), is_slave=is_slave, now=now)
                        for o in ordered
                    ],
                    "anyStopLimit": any(o.type.is_stop_limit for o in ordered),
                }
        return out

    @staticmethod
    def _terminal_raw(raw: dict[str, Any]) -> dict[str, str | bool]:
        """The page's fields as apply_edits reads them. Only known names pass -- an extra key
        is a page bug, not a reason to refuse a save."""
        out: dict[str, str | bool] = {}
        for name, value in raw.items():
            if name in BY_NAME:
                out[name] = value if isinstance(value, bool) else str(value)
        return out

    def terminal_settings_check(self, tid: str, raw: dict[str, Any]) -> dict[str, Any]:
        """Per-field errors, what a save would change, and the worked example, as the person
        types. Nothing is saved."""
        tid = str(tid)
        tcfg = self._cfg.terminals.get(tid)
        if tcfg is None:
            return {"errors": {"": "that terminal is gone; rescan"}, "changes": ""}
        outcome = apply_edits(tcfg, self._terminal_raw(raw))
        out: dict[str, Any] = {
            "errors": outcome.errors,
            "changes": describe_changes(outcome.changes, limit=160) if outcome.changes else "",
        }
        if tcfg.role is Role.SLAVE:
            out["example"] = self._example_dict(tid, outcome.config)
        elif tcfg.role is Role.MASTER:
            out["symbolExample"] = self._symbol_example(
                self._master_symbols(tid),
                str(raw.get("symbol_prefix", "")),
                str(raw.get("symbol_suffix", "")),
            )
        return out

    def terminal_settings_save(self, tid: str, raw: dict[str, Any]) -> dict[str, Any]:
        """Save the form, the TUI's own path: apply_edits rules on it, the engine swaps it in
        atomically, and open copies stay managed -- only unassigning or disabling orphans."""
        tid = str(tid)
        cfg = self._cfg
        tcfg = cfg.terminals.get(tid)
        if tcfg is None:
            return {"ok": False, "message": "that terminal is gone; rescan"}
        outcome = apply_edits(tcfg, self._terminal_raw(raw))
        if not outcome.ok:
            return {
                "ok": False,
                "errors": outcome.errors,
                "message": "Correct the marked values; nothing was changed.",
            }
        new = outcome.config
        assert new is not None
        if not outcome.changes:
            return {
                "ok": True,
                "messages": [{"text": f"{cfg.display_name(tid)}: no changes.", "tone": ""}],
            }
        refusal = self._engine.apply_settings(new)
        if refusal is not None:
            return {"ok": False, "errors": {}, "message": refusal}
        log.info(
            "settings changed for %s (%s): %s",
            new.label,
            tid[:8],
            describe_changes(outcome.changes),
        )
        messages = [
            {
                "text": f"{cfg.display_name(tid)}: {describe_changes(outcome.changes, limit=160)}.",
                "tone": "ok",
            }
        ]
        real = 0 if cfg.dry_run else self._engine.copies_on_real_account(tid)
        for warning in advisories(outcome, real):
            messages.append({"text": warning, "tone": "warn"})
        why = self._persist()
        if why is not None:
            messages.append({"text": why, "tone": "bad"})
        return {"ok": True, "messages": messages}

    def terminal_settings_from(self, source_tid: str) -> dict[str, Any]:
        """Another slave's settings to fill the form from, for review: its symbol prefix,
        suffix and renames are its broker's, so they stay out. Nothing is saved here."""
        src = self._cfg.terminals.get(str(source_tid))
        if src is None or src.role is not Role.SLAVE:
            return {"ok": False, "message": "that slave is gone; pick another"}
        values = initial_values(src)
        for name in self._BROKERS_OWN:
            values.pop(name, None)
        return {
            "ok": True,
            "values": values,
            "name": self._cfg.display_name(src.terminal_id),
        }


_HISTORY_SHOWN = 100
"""Cards sent to the page at most per answer; the counts say what the filter left out."""


def _detail_trades(
    master_positions: list[MasterTrade], events_: list[MasterEvent]
) -> list[MasterTrade]:
    """The master's trades for the worked example, newest first, each once: what it holds,
    then what the day book saw -- tui/settings.py's _trades."""
    seen: set[int] = set()
    out: list[MasterTrade] = []
    for t in master_positions:
        if t.ticket not in seen:
            seen.add(t.ticket)
            out.append(t)
    for e in reversed(events_):
        if e.kind not in (ActionKind.OPEN, ActionKind.PLACE) or e.master_ticket in seen:
            continue
        if e.entry <= 0:
            continue
        seen.add(e.master_ticket)
        out.append(
            MasterTrade(e.master_ticket, e.symbol, e.label.startswith("buy"), e.entry, e.sl, e.tp)
        )
    return out


def _step_tone(step: Step) -> str:
    """The colour tui/history.py's step_color gives the step, named for the page to style
    by: a note blue, a problem red, a skip yellow, a dry run or a followed fill dim."""
    if step.kind == NOTE:
        return "note"
    if step.problem:
        return "bad"
    if step.outcome in (Outcome.SKIPPED.value, Outcome.NOT_COPIED.value):
        return "warn"
    if step.outcome in (Outcome.DRY_RUN.value, Outcome.FOLLOWS.value):
        return "dim"
    return ""


def _news_counts(p: dict[str, ParamValue], e: NewsEvent) -> bool:
    """Whether this rule counts this release -- the rule's own impact arithmetic."""
    impacts = {HIGH} if p["impact"] == "high" else {HIGH, MEDIUM}
    return e.impact in impacts


def _news_said(p: dict[str, ParamValue], e: NewsEvent) -> str:
    """One rule's answer to one release, worded like the design: the action and when, or
    the no-new-trades window when the rule only declines."""

    def clock(ts: float) -> str:
        return time.strftime("%H:%M", time.localtime(ts))

    scoped = p["affects"] == "currency" and e.currency != "ALL"
    if p["before_news"] != "nothing":
        acts = clock(e.when - float(p["act_before"]) * 60)
        what = (
            "breakeven open copies, close losers"
            if p["before_news"] == "breakeven"
            else "close open copies"
        )
        resting = f"cancel resting {e.currency}" if scoped else "cancel all resting"
        return f"acts {acts} · {what}, {resting}"
    start = clock(e.when - float(p["before"]) * 60)
    end = clock(e.when + float(p["after"]) * 60)
    which = f"{e.currency} " if scoped else ""
    return f"no new {which}trades {start}–{end}"


def _profile_age(ts: float | None) -> str:
    """When a profile last ran, the TUI picker's own words."""
    if ts is None:
        return "never opened"
    days = max(0, int((time.time() - ts) // 86400))
    if days == 0:
        return time.strftime("today %H:%M", time.localtime(ts))
    if days == 1:
        return "yesterday"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# --- the detail page's Overview tab -----------------------------------------------------------
# The TUI enter page's three ruled columns and its tables, as JSON. The wording is ported from
# tui/detail.py, which stays the reference; every number comes from mt5copier/terminal_detail.py
# and copy_stats.py, the same UI-free helpers the TUI reads. Tones are the app's palette words.

_TIER_TONE = {
    "good": "",
    "warning": "yellow",
    "serious": "red",
    "critical": "red",
    "muted": "muted",
}


def _ov_row(label: str, text: str, tone: str = "") -> dict[str, str]:
    return {"label": label, "text": text, "tone": tone}


def _ov_account(snap: Snapshot | None) -> dict[str, Any] | None:
    """The ACCOUNT column's numbers; the page formats and draws the gauge."""
    if snap is None:
        return None
    gauge = margin_gauge(snap)
    pl = (
        snap.equity - snap.balance if snap.equity is not None and snap.balance is not None else None
    )
    return {
        "balance": snap.balance,
        "floating": pl,
        "equity": snap.equity,
        "margin": snap.margin,
        "marginFree": gauge.free,
        "usedFraction": gauge.used_fraction,
        "levelPct": gauge.level_pct,
        "levelTier": gauge.level_tier,
        "noMargin": snap.margin == 0,
    }


def _ov_example_symbol(views: list[TerminalView]) -> str:
    """A symbol the master holds, to show how its name changes here; EURUSD if none."""
    master = next((v for v in views if v.cfg.role is Role.MASTER), None)
    held = master.snapshot.open_positions if master is not None and master.snapshot else None
    return held[0].symbol if held else "EURUSD"


def _ov_risk_text(tcfg: TerminalConfig, currency: str | None) -> str:
    if tcfg.sizing_mode is SizingMode.FIXED_CASH_RISK:
        return f"{tcfg.risk_cash:,g} {currency or ''}".rstrip() + " a trade"
    return f"{tcfg.risk_percent:g}% of balance"


def _ov_few(names: Sequence[str]) -> str:
    shown = ", ".join(names[:3])
    return shown + (f" +{len(names) - 3}" if len(names) > 3 else "")


def _ov_symbol_rows(tcfg: TerminalConfig, example: str) -> list[dict[str, str]]:
    """Which of the master's symbols it takes, and the name each is traded under here."""
    if tcfg.symbol_whitelist:
        which = f"only {_ov_few(tcfg.symbol_whitelist)}"
    elif tcfg.symbol_blacklist:
        which = f"all except {_ov_few(tcfg.symbol_blacklist)}"
    else:
        which = "all"
    rows = [_ov_row("Symbols copied", which)]
    if tcfg.symbol_whitelist and tcfg.symbol_blacklist:
        rows.append(_ov_row("  never", _ov_few(tcfg.symbol_blacklist)))
    # A renamed symbol keeps its own name, so the example is one the decoration applies to.
    example = example.upper()
    if example in tcfg.symbol_map:
        example = next((s for s in ("EURUSD", "XAUUSD") if s not in tcfg.symbol_map), example)
    if tcfg.symbol_prefix or tcfg.symbol_suffix:
        here = f"{tcfg.symbol_prefix}{example}{tcfg.symbol_suffix}"
        rows.append(_ov_row("Name on this broker", f"{example} is traded as {here}"))
    else:
        rows.append(_ov_row("Name on this broker", "the same as the master's"))
    renamed = sorted(tcfg.symbol_map.items())
    for n, (master, here) in enumerate(renamed[:3]):
        rows.append(_ov_row("Renamed" if n == 0 else "", f"{master} is traded as {here}"))
    if len(renamed) > 3:
        rows.append(_ov_row("", f"+{len(renamed) - 3} more (Settings tab)", "muted"))
    return rows


def _ov_rule_rows(
    tcfg: TerminalConfig, currency: str | None, example: str, *, managed: bool
) -> list[dict[str, str]]:
    """How trades are copied to it, in the words of its settings form."""
    if tcfg.no_stop_policy is NoStopPolicy.SKIP:
        no_stop = "skip the trade"
    else:
        no_stop = f"copy {tcfg.no_stop_lot_multiple:g}x its lots"
    rule = breakeven_rule(tcfg)
    if managed:
        be = "the master's stop moves"
    else:
        be = "off" if rule == "off" else f"at {rule}"
    rows = [_ov_row("Risk per trade", _ov_risk_text(tcfg, currency))]
    if tcfg.max_lot is not None:
        rows.append(_ov_row("Largest lot", f"{tcfg.max_lot:g}"))
    rows += [
        _ov_row("Master has no stop", no_stop),
        _ov_row(
            "Direction",
            "reversed" if tcfg.invert else "same as master",
            "yellow" if tcfg.invert else "",
        ),
        _ov_row(
            "Once open",
            "the master manages it: stop and target moves, partial closes"
            if managed
            else "this slave manages it",
        ),
        _ov_row("Pending orders", "copied" if tcfg.copy_pending else "not copied"),
        _ov_row(
            "Refused pending price",
            "retried as the other type, then at market" if tcfg.pending_fallback else "not copied",
        ),
        _ov_row("Breakeven", be),
    ]
    rows += _ov_symbol_rows(tcfg, example)
    if tcfg.allow_live_trading:
        rows.append(_ov_row("Live trading", "allowed", "red"))
    return rows


def _ov_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


def _ov_slippage(value: float | None, *, colour: bool = True) -> tuple[str, str]:
    """Points from the master's price: ``1,175 pts worse`` in red, or better in blue."""
    if value is None:
        return "—", "muted"
    if round(value) == 0:
        return "none", ""
    if value > 0:
        return f"{value:,.0f} pts worse", "red" if colour else ""
    return f"{-value:,.0f} pts better", "blue" if colour else ""


def _ov_disconnects(stats: TerminalStats) -> tuple[str, str]:
    n = stats.disconnects
    return ("none", "muted") if n == 0 else (f"{n:,}", "yellow")


def _ov_copying_rows(stats: TerminalStats) -> list[dict[str, str]]:
    """How copying to a slave has gone since the copier started."""
    ok = stats.succeeded
    rows = [_ov_row("Orders copied", f"{ok:,}" if ok else "none", "blue" if ok else "muted")]
    if stats.skipped_total:
        rows.append(_ov_row("Skipped", f"{stats.skipped_total:,}"))
        reason, n = stats.skipped[0]
        rows.append(_ov_row("  most often", f"{reason} x{n}", "muted"))
    else:
        rows.append(_ov_row("Skipped", "none", "muted"))
    trouble = (stats.refused, stats.retried, stats.dropped_late)
    if any(trouble):
        rows.append(
            _ov_row(
                "Refused, retried, late",
                " / ".join(f"{n:,}" for n in trouble),
                "red" if stats.refused else "",
            )
        )
    else:
        rows.append(_ov_row("Refused, retried, late", "none", "muted"))
    lost = (stats.failed_transient, stats.expired)
    rows.append(
        _ov_row("Failed, unanswered", " / ".join(f"{n:,}" for n in lost), "yellow")
        if any(lost)
        else _ov_row("Failed, unanswered", "none", "muted")
    )
    delays = (stats.latency_last, stats.latency_median, stats.latency_max)
    rows.append(
        _ov_row("Delay, last / median / max", " / ".join(_ov_ms(d) for d in delays) + " ms")
        if delays[0] is not None
        else _ov_row("Delay, last / median / max", "—", "muted")
    )
    for label, value, colour in (
        ("Slippage, last", stats.slippage_last, True),
        ("Slippage, average", stats.slippage_mean, False),
        ("Slippage, worst", stats.slippage_worst, True),
    ):
        text, tone = _ov_slippage(value, colour=colour)
        rows.append(_ov_row(label, text, tone))
    text, tone = _ov_disconnects(stats)
    rows.append(_ov_row("Disconnects", text, tone))
    return rows


def _ov_connection_rows(stats: TerminalStats, snap: Snapshot | None) -> list[dict[str, str]]:
    """The master and terminals with no role send nothing, so there is no copying to report:
    only how their link is holding."""
    text, tone = _ov_disconnects(stats)
    ping = format_ping(snap.ping_last_us if snap is not None else None)
    return [
        _ov_row("Connected for", format_duration(stats.connected_for_s)),
        _ov_row("Disconnects", text, tone),
        _ov_row("Ping", ping, "muted" if ping == "—" else ""),
    ]


def _ov_exposure(positions: tuple[Position, ...] | None) -> str | None:
    """``BTCUSD long 3.11 lots (gross 4.11), +2 more`` for the positions heading; ``flat``
    when nothing is open, None when the positions could not be read."""
    if positions is None:
        return None
    exp = exposure(positions)
    if not exp:
        return "flat"
    bits = []
    for e in exp[:3]:
        side = "long" if e.net_lots > 0 else ("short" if e.net_lots < 0 else "flat")
        bit = f"{e.symbol} {side} {abs(e.net_lots):g} lots"
        if e.gross_lots != abs(e.net_lots):
            bit += f" (gross {e.gross_lots:g})"
        bits.append(bit)
    if len(exp) > 3:
        bits.append(f"+{len(exp) - 3} more")
    return ", ".join(bits)


def _ov_price(value: float, digits: int) -> str:
    return f"{value:.{digits}f}" if value else "—"


def _ov_r(r: PositionRow, copy: OpenCopy | None) -> tuple[float | None, float | None]:
    """How far the price is from entry now and where the target is, both in R: a copy's
    opening stop distance when it is known, else the stop as it stands."""
    sign = 1.0 if r.side == "BUY" else -1.0
    if copy is not None and copy.risk_distance > 0:
        risk = copy.risk_distance
    elif r.sl:
        risk = max((r.price_open - r.sl) * sign, 0.0)
    else:
        risk = 0.0
    if risk <= 0 or r.price_current is None:
        return None, None
    now = (r.price_current - r.price_open) * sign / risk
    target = (r.tp - r.price_open) * sign / risk if r.tp else None
    return round(now, 2), round(target, 2) if target is not None else None


def _ov_breakeven(
    r: PositionRow, copy: OpenCopy | None, tcfg: TerminalConfig
) -> dict[str, str] | None:
    """What will move this copy's stop to breakeven, and where; None for a row that is not
    one of this slave's copies, whose cell stays blank."""
    if copy is None:
        return None
    if copy.breakeven_exempt:
        return {"text": "exempt", "tone": "muted"}
    if copy.breakeven_sl:
        return {"text": f"✓ stop at {_ov_price(copy.breakeven_sl, r.digits)}", "tone": "green"}
    if copy.breakeven_at:
        return {"text": f"at {_ov_price(copy.breakeven_at, r.digits)}", "tone": ""}
    if tcfg.breakeven_mode is BreakevenMode.R:
        times = f"{tcfg.breakeven_trigger_r:g}R"
        if copy.risk_distance <= 0:
            return {"text": f"{times} · opened with no stop", "tone": "muted"}
        sign = 1.0 if r.side == "BUY" else -1.0
        at = r.price_open + sign * tcfg.breakeven_trigger_r * copy.risk_distance
        return {"text": f"{times} at {_ov_price(at, r.digits)}", "tone": ""}
    return {"text": "off", "tone": "muted"}


def _ov_position_row(
    r: PositionRow, copy: OpenCopy | None, tcfg: TerminalConfig, *, is_slave: bool
) -> dict[str, Any]:
    r_now, r_target = _ov_r(r, copy)
    return {
        "ticket": r.ticket,
        "symbol": r.symbol,
        "side": "buy" if r.side == "BUY" else "sell",
        "lots": r.volume,
        "digits": r.digits,
        "open": r.price_open,
        "now": r.price_current,
        "sl": r.sl,
        "tp": r.tp,
        "slHit": r.sl_room is not None and r.sl_room <= 0,
        "tpHit": r.tp_room is not None and r.tp_room <= 0,
        "rNow": r_now,
        "rTarget": r_target,
        "swap": r.swap,
        "profit": r.profit,
        # The column is headed "Copy of", so the cell need not say it again.
        "link": {
            "text": r.link.removeprefix("copy of "),
            "tone": _TIER_TONE.get(r.link_tier, ""),
        },
        "breakeven": _ov_breakeven(r, copy, tcfg) if is_slave else None,
    }


def _ov_order_row(
    o: PendingOrder, copy: OpenCopy | None, *, is_slave: bool, now: float
) -> dict[str, Any]:
    """One resting order. Its age rather than its time: the broker's clock offset cancels
    out of a difference, near enough for minutes."""
    digits = price_digits(o.price_open, o.sl, o.tp, o.price_stoplimit)
    link = None
    if is_slave:
        if copy is not None:
            link = {"text": f"#{copy.master_ticket}", "tone": ""}
        elif o.magic == COPIER_MAGIC:
            link = {"text": "ours, untracked", "tone": "yellow"}
        else:
            link = {"text": "not the copier's", "tone": "muted"}
    placed = None
    if o.time_setup:
        placed = format_duration(max(now - o.time_setup, 0.0)) + " ago"
    return {
        "ticket": o.ticket,
        "symbol": o.symbol,
        "kind": o.type.name.lower().replace("_", " "),
        "side": "buy" if o.type.direction is PositionType.BUY else "sell",
        "lots": o.volume,
        "digits": digits,
        "price": o.price_open,
        "stopLimit": o.price_stoplimit,
        "sl": o.sl,
        "tp": o.tp,
        "placed": placed,
        "link": link,
    }
