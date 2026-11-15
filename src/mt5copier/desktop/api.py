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
from ..config import AppMode, Role, SizingMode, TerminalConfig
from ..discovery import discover_terminals
from ..guards import Level
from ..logging_setup import get_logger
from ..paths import active_profile
from ..presentation import row_info, slave_badge
from ..protocol import CHART_TIMEFRAMES, OrderType, PendingOrder, Position, PositionType
from . import winframe

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

    def __init__(self, cfg: AppConfig, engine: Engine) -> None:
        self._cfg = cfg
        self._engine = engine
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
            {str(t): [int(n) for n in tickets] for t, tickets in agreed.items()}
            if agreed
            else None
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
