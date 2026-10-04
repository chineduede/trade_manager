"""Render the TUI headlessly against real discovery output and dump what it draws.

Safety: the Engine is replaced with a non-spawning stub, so this launches no MT5 terminal and
opens no connection even though roles are assigned to draw master and slave cards. Assigning a
role with the real engine would call initialize() and cold-start those terminals.

Usage:  uv run python scripts/render_check.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from mt5copier import config as config_mod
from mt5copier.config import AppConfig, Role
from mt5copier.copy_stats import TerminalStats
from mt5copier.discovery import discover_terminals
from mt5copier.engine import TerminalView
from mt5copier.protocol import Snapshot, WorkerState
from mt5copier.tui import app as app_mod


class NoSpawnEngine:
    """Engine stand-in that fabricates plausible snapshots instead of connecting."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def sync(self) -> None: ...
    def restart(self, terminal_id: str) -> None: ...
    def reconnect(self, terminal_id: str) -> None: ...

    def views(self) -> list[TerminalView]:
        views = []
        for i, t in enumerate(self._cfg.terminals.values()):
            snap = None
            if t.role is not Role.UNASSIGNED:
                snap = Snapshot(
                    terminal_id=t.terminal_id,
                    state=WorkerState.CONNECTED if i % 4 else WorkerState.DISCONNECTED,
                    equity=10_204.11 * (i + 1),
                    balance=10_000.0 * (i + 1),
                    currency="USD",
                    positions=i % 3,
                    leverage=100,
                    trade_allowed=True,
                )
            views.append(
                TerminalView(
                    cfg=t.model_copy(deep=True),
                    snapshot=snap,
                    received_at=None if snap is None else __import__("time").monotonic(),
                    process_alive=snap is not None,
                    supervised=snap is not None,
                )
            )
        views.sort(key=lambda v: (v.cfg.label.lower(), v.cfg.terminal_id))
        return views

    def holds(self) -> dict[str, str]:
        return {}

    def orphans(self, terminal_id: str | None = None) -> list[object]:
        return []

    def terminal_stats(self, terminal_id: str) -> TerminalStats:
        return TerminalStats()

    def day_book(self, limit: int | None = None) -> list[object]:
        return []

    day_book_since = 0.0
    blocked_reason = None
    failed_total = 0

    def health(self) -> dict[str, int]:
        views = self.views()
        return {
            "total": len(views),
            "supervised": sum(1 for v in views if v.supervised),
            "live": sum(1 for v in views if v.snapshot and v.snapshot.is_live),
            "stale": 0,
        }


async def main() -> None:
    app_mod.Engine = NoSpawnEngine  # type: ignore[misc,assignment]

    cfg = AppConfig()
    config_mod.merge_discovery(cfg, discover_terminals())

    usable = [t for t in cfg.terminals.values() if t.exe_path is not None]
    if len(usable) >= 3:
        cfg.set_role(usable[0].terminal_id, Role.MASTER)
        cfg.set_role(usable[1].terminal_id, Role.SLAVE)
        cfg.set_role(usable[2].terminal_id, Role.SLAVE)
        cfg.terminals[usable[1].terminal_id].risk_percent = 0.5

    app = app_mod.CopierApp(cfg)
    async with app.run_test(size=(110, 26)) as pilot:
        await pilot.pause()
        svg = Path("scripts/render_check.svg")
        app.save_screenshot(str(svg))
        print(f"rendered ok -> {svg}   terminals={len(app._order)}\n")

        from rich.console import Console

        console = Console(width=110)
        console.print(app.topbar())
        for card in app._cards.values():
            console.print(card.content)


if __name__ == "__main__":
    asyncio.run(main())
