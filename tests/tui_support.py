"""What the UI tests share: a stub engine that spawns nothing, and the terminals they act on.

The Engine is stubbed out, so the tests spawn no processes and touch no MT5 terminal.
"""

from __future__ import annotations

import io
import time
from collections.abc import Collection, Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console

from mt5copier.config import BREAKEVEN_FIELDS, AppConfig, Role, TerminalConfig, account_key
from mt5copier.copy_stats import TerminalStats
from mt5copier.day_book import MasterEvent
from mt5copier.engine import RecordedAction, RecordedResult, TerminalView
from mt5copier.guards import GuardBook, GuardReport, GuardsDoc
from mt5copier.protocol import (
    COPIER_MAGIC,
    Position,
    PositionLive,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.reconcile import OpenCopy
from mt5copier.settings_edit import copies_word
from mt5copier.store import Orphan
from mt5copier.tui import CopierApp

TID_MASTER = "A" * 32
TID_SLAVE = "B" * 32
TID_BROKEN = "C" * 32


class StubEngine:
    """Engine stand-in: records calls, spawns nothing."""

    def __init__(
        self, cfg: AppConfig, state_path: Path | None = None, guards: GuardsDoc | None = None
    ) -> None:
        self._cfg = cfg
        # A state file that never exists: the stub records nothing at a reset.
        self.guard_book = GuardBook(guards or GuardsDoc(), Path("no-such-dir/guard_state.json"))
        self.sync_calls = 0
        self.restarted: list[str] = []
        self.reconnected: list[str] = []
        self.started = False
        self.stopped = False
        self.cleared = 0
        self.held: dict[str, str] = {}
        self.found_orphans: dict[str, list[Orphan]] = {}
        self.acknowledged: list[str] = []
        self.results: list[RecordedResult] = []
        self.copies: dict[str, list[OpenCopy] | None] = {}
        """What the stub says each slave holds. ``None`` is a read that failed."""
        self.given_up: list[tuple[str, list[int]]] = []
        """Each time the engine was agreed a slave's copies could be given up: (slave, tickets)."""
        self.snapshots: dict[str, Snapshot] = {}
        self.stats: dict[str, TerminalStats] = {}
        self.events: list[MasterEvent] = []
        """What the stub's day book holds, oldest first."""

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def sync(self) -> None:
        self.sync_calls += 1

    def restart(self, terminal_id: str) -> None:
        self.restarted.append(terminal_id)

    def reconnect(self, terminal_id: str) -> None:
        self.reconnected.append(terminal_id)

    def views(self) -> list[TerminalView]:
        views = [
            TerminalView(cfg=t.model_copy(deep=True), snapshot=self.snapshots.get(t.terminal_id))
            for t in self._cfg.terminals.values()
        ]
        views.sort(key=lambda v: (v.cfg.label.lower(), v.cfg.terminal_id))
        return views

    def health(self) -> dict[str, int]:
        return {"total": len(self._cfg.terminals), "supervised": 0, "live": 0, "stale": 0}

    # --- Stage 2a detection surface ---------------------------------------------------------

    def recent_actions(self, limit: int | None = None) -> list[RecordedAction]:
        return []

    def day_book(self, limit: int | None = None) -> list[MasterEvent]:
        return self.events if limit is None else self.events[-limit:]

    day_book_since = 0.0

    @property
    def action_total(self) -> int:
        return 0

    @property
    def blocked_reason(self) -> str | None:
        return None

    def believed_copies(self) -> int:
        return 0

    def clear_detection_state(self) -> None:
        self.cleared += 1

    # --- Stage 2c startup reconciliation ----------------------------------------------------

    def holds(self) -> dict[str, str]:
        return dict(self.held)

    def orphans(self, terminal_id: str | None = None) -> list[Orphan]:
        if terminal_id is not None:
            return list(self.found_orphans.get(terminal_id, []))
        return [o for items in self.found_orphans.values() for o in items]

    def acknowledge_orphans(self, terminal_id: str) -> int:
        self.acknowledged.append(terminal_id)
        found = self.found_orphans.get(terminal_id, [])
        self.held.pop(terminal_id, None)
        return len(found)

    # --- Stage 2c dispatch ------------------------------------------------------------------

    def recent_results(self, limit: int | None = None) -> list[RecordedResult]:
        return list(self.results) if limit is None else list(self.results)[-limit:]

    @property
    def failed_total(self) -> int:
        return sum(1 for r in self.results if not r.result.ok)

    # --- the settings editor ----------------------------------------------------------------

    def clear_guard_latch(self, terminal_id: str, rule_id: str) -> bool:
        key = self.guard_report(terminal_id).account_key
        return key is not None and self.guard_book.clear_latch(key, rule_id)

    def guard_report(self, terminal_id: str) -> GuardReport:
        snap = self.snapshots.get(terminal_id)
        cfg = self._cfg.terminals.get(terminal_id)
        if snap is not None and snap.login is not None:
            key = account_key(snap.login, snap.server)
        else:
            key = account_key(cfg.last_login, cfg.last_server) if cfg else None
        return self.guard_book.report(key, snap, time.time())

    def terminal_stats(self, terminal_id: str) -> TerminalStats:
        return self.stats.get(terminal_id, TerminalStats())

    def open_copies(self, terminal_id: str) -> list[OpenCopy] | None:
        copies = self.copies.get(terminal_id, [])
        return None if copies is None else list(copies)

    def copies_at_stake(
        self, terminal_id: str, new: TerminalConfig | None = None
    ) -> dict[str, list[OpenCopy]] | None:
        """The real engine's rule: a slave stakes its own copies, unless only its breakeven
        settings changed; the master stakes every slave's when unassigned (``new`` None) or
        disabled, and nothing otherwise."""
        cfg = self._cfg.terminals[terminal_id]
        if cfg.role is not Role.MASTER:
            only_be = new is not None and all(
                getattr(cfg, f) == getattr(new, f)
                for f in TerminalConfig.model_fields
                if f not in BREAKEVEN_FIELDS
            )
            ids = [] if only_be else [terminal_id]
        elif new is None or (cfg.enabled and not new.enabled):
            ids = [t.terminal_id for t in self._cfg.slaves]
        else:
            ids = []
        stake: dict[str, list[OpenCopy]] = {}
        for tid in ids:
            held = self.open_copies(tid)
            if held is None:
                return None
            if held:
                stake[tid] = held
        return stake

    def _give_up(
        self, stake: dict[str, list[OpenCopy]] | None, orphan: Mapping[str, Collection[int]] | None
    ) -> str | None:
        """The real engine's contract: copies are only given up when the ones agreed to are
        exactly the ones held now."""
        if stake is None:
            return "its open copies could not be read, so they cannot be given up safely"
        if not stake:
            return None
        if orphan is None:
            total = sum(len(h) for h in stake.values())
            return f"{copies_word(total)} would be orphaned; that has to be confirmed"
        agreed = {t: set(tickets) for t, tickets in orphan.items() if tickets}
        if agreed != {t: {c.master_ticket for c in h} for t, h in stake.items()}:
            return "its open copies changed while you were deciding; try again"
        for tid in stake:
            self.given_up.append((tid, sorted(agreed[tid])))
            self.copies[tid] = []
        return None

    def apply_settings(
        self, new: TerminalConfig, *, orphan: Mapping[str, Collection[int]] | None = None
    ) -> str | None:
        refusal = self._give_up(self.copies_at_stake(new.terminal_id, new), orphan)
        if refusal is not None:
            return refusal
        self._cfg.replace_terminal(new)
        return None

    def set_paused(self, terminal_id: str, paused: bool) -> None:
        cfg = self._cfg.terminals[terminal_id]
        self._cfg.replace_terminal(cfg.model_copy(update={"paused": paused}))

    def _change_copy(self, terminal_id: str, master_ticket: int, **changes: object) -> bool:
        held = self.copies.get(terminal_id) or []
        for i, c in enumerate(held):
            if c.master_ticket == master_ticket and not c.resting:
                held[i] = replace(c, **changes)  # type: ignore[arg-type]
                return True
        return False

    def set_breakeven_exempt(self, terminal_id: str, master_ticket: int, exempt: bool) -> bool:
        extra = {"breakeven_sl": 0.0, "breakeven_at": 0.0} if exempt else {}
        return self._change_copy(terminal_id, master_ticket, breakeven_exempt=exempt, **extra)

    def set_breakeven_at(self, terminal_id: str, master_ticket: int, price: float) -> str | None:
        if price and price < 1.0851:
            return "for a buy it has to be above 1.0851 (entry plus buffer)"
        extra = {"breakeven_exempt": False} if price else {}
        self._change_copy(terminal_id, master_ticket, breakeven_at=price, **extra)
        return None

    def unassign(
        self, terminal_id: str, *, orphan: Mapping[str, Collection[int]] | None = None
    ) -> str | None:
        from mt5copier import role_actions

        refusal = self._give_up(self.copies_at_stake(terminal_id), orphan)
        if refusal is not None:
            return refusal
        role_actions.unassign(self._cfg, terminal_id)
        return None


def make_cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        terminals={
            TID_MASTER: TerminalConfig(
                terminal_id=TID_MASTER, label="Alpha Broker", exe_path=exe, last_login=111
            ),
            TID_SLAVE: TerminalConfig(
                terminal_id=TID_SLAVE, label="Beta Broker", exe_path=exe, last_login=222
            ),
            # No exe_path: an uninstalled leftover, like the EXNESS install on this machine.
            TID_BROKEN: TerminalConfig(
                terminal_id=TID_BROKEN, label="Gone Broker", exe_path=None, last_login=333
            ),
        }
    )


def make_app(cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CopierApp:
    """App wired to the stub engine, saving to a temp config file."""
    monkeypatch.setattr("mt5copier.tui.app.Engine", StubEngine)
    saved = tmp_path / "config.json"
    monkeypatch.setattr("mt5copier.config.config_path", lambda: saved)
    monkeypatch.setattr("mt5copier.tui.app.config_path", lambda: saved)
    monkeypatch.setattr("mt5copier.tui.app.guards_path", lambda: tmp_path / "guards.json")
    instance = CopierApp(cfg)
    instance._saved_to = saved  # type: ignore[attr-defined]
    return instance


def wire_copying(app: CopierApp, cfg: AppConfig) -> None:
    """A master with one position, and a slave holding its copy plus an unmanaged one."""
    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    engine = app._engine
    engine.snapshots[TID_MASTER] = Snapshot(  # type: ignore[attr-defined]
        terminal_id=TID_MASTER,
        state=WorkerState.CONNECTED,
        balance=10_000.0,
        equity=10_120.0,
        currency="USD",
        open_positions=(
            Position(
                ticket=100,
                symbol="EURUSD",
                type=PositionType.BUY,
                volume=1.0,
                price_open=1.08500,
                sl=1.08000,
            ),
        ),
        live_positions=(PositionLive(ticket=100, price_current=1.08620, profit=120.0),),
    )
    engine.snapshots[TID_SLAVE] = Snapshot(  # type: ignore[attr-defined]
        terminal_id=TID_SLAVE,
        state=WorkerState.CONNECTED,
        trade_mode=0,
        balance=5_000.0,
        equity=5_040.0,
        margin=500.0,
        margin_level=1_008.0,
        margin_free=4_540.0,
        currency="USD",
        ping_last_us=41_250,
        open_positions=(
            Position(
                ticket=5001,
                symbol="EURUSD",
                type=PositionType.BUY,
                volume=0.5,
                price_open=1.08504,
                sl=1.08000,
                magic=COPIER_MAGIC,
            ),
            Position(
                ticket=6000, symbol="GBPUSD", type=PositionType.SELL, volume=0.2, price_open=1.27000
            ),
        ),
        live_positions=(
            PositionLive(ticket=5001, price_current=1.08620, profit=58.0, swap=-0.4),
            PositionLive(ticket=6000, price_current=1.27100, profit=-20.0),
        ),
    )
    engine.copies[TID_SLAVE] = [  # type: ignore[attr-defined]
        OpenCopy(master_ticket=100, slave_ticket=5001, symbol="EURUSD", volume=0.5, resting=False)
    ]
    engine.stats[TID_SLAVE] = TerminalStats(  # type: ignore[attr-defined]
        latencies_ms=(120.0, 80.0, 95.0),
        slippage_points=(4.0,),
        succeeded=3,
        refused=1,
        connected_for_s=125.0,
    )


def copy_of(
    master_ticket: int,
    slave_ticket: int | None = None,
    symbol: str = "EURGBPp",
    volume: float = 0.5,
    resting: bool = False,
) -> OpenCopy:
    ticket = master_ticket + 1000 if slave_ticket is None else slave_ticket
    return OpenCopy(master_ticket, ticket, symbol, volume, resting)


def hold(app: CopierApp, *copies: OpenCopy, terminal_id: str = TID_SLAVE) -> None:
    app._engine.copies[terminal_id] = list(copies)  # type: ignore[attr-defined]


def toasts(app: CopierApp) -> list[str]:
    return [n.message for n in app._notifications]


def plain(renderable: object, width: int = 160) -> str:
    """What a renderable reads as, without its colours."""
    console = Console(width=width, file=io.StringIO(), color_system=None)
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[attr-defined]
