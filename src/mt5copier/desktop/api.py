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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .. import config as config_mod
from .. import role_actions
from ..config import AppMode, Role, SizingMode, TerminalConfig, account_key
from ..discovery import discover_terminals
from ..guards import REGISTRY, GuardRule, GuardsDoc, Level
from ..guards import model as guards_model
from ..guards.base import money
from ..guards.params import ZONE_HINT, ParamKind, ParamValue
from ..logging_setup import get_logger
from ..news import HIGH, MEDIUM, symbol_currencies
from ..paths import active_profile, guards_path
from ..presentation import row_info, slave_badge
from ..protocol import CHART_TIMEFRAMES, OrderType, PendingOrder, Position, PositionType
from ..terminal_detail import margin_gauge
from . import winframe

if TYPE_CHECKING:
    import webview

    from ..config import AppConfig
    from ..engine import Engine, TerminalView
    from ..guards import RuleStatus
    from ..history import GuardAction
    from ..news import NewsEvent

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

    def __init__(self, cfg: AppConfig, engine: Engine, guards: GuardsDoc | None = None) -> None:
        self._cfg = cfg
        self._engine = engine
        # The same document the engine enforces from (see tui.run and desktop.run): edits
        # here are one assignment into it, and the file is written after, as the TUI does.
        self._guards_doc = guards if guards is not None else GuardsDoc()
        self._window: webview.Window | None = None
        self._maximized = False

    def attach(self, window: webview.Window) -> None:
        self._window = window

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
        return {
            "tid": tid,
            "name": info.label,
            "role": str(tcfg.role),
            "statusText": info.status_text,
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
