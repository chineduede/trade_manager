"""The history page: ``h`` on a slave. A card for each trade copied to it, newest first, kept
for the days set in the copier's settings: what was copied, and under it every step taken with
it since -- a partial close, a stop moved, a breakeven, a change declined and why, closed.

What is kept and for how long is :mod:`mt5copier.history`'s; this only draws it. The functions
here decide what each card says, so the tests can read them without starting the app.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Static

from ..day_book import Outcome
from ..history import NOTE, Step, Trade
from .style import BLUE, FG, MUTED, RED, YELLOW, key_hint, left_right, spaced
from .widgets import KeyBar, TopBar

if TYPE_CHECKING:
    from .app import CopierApp

SHOWN = 100
"""Cards drawn at most; the rest of what is kept is counted, not drawn."""


def when(ts: float, now: float | None = None) -> str:
    """``23:08:15`` today, ``04 Oct 23:08`` before it, in local time."""
    now = time.time() if now is None else now
    here = time.localtime(ts)
    if time.strftime("%Y%m%d", here) == time.strftime("%Y%m%d", time.localtime(now)):
        return time.strftime("%H:%M:%S", here)
    return time.strftime("%d %b %H:%M", here)


def card_title(trade: Trade) -> str:
    entry = trade.entry
    lots = f" {entry.lots:g} lots" if entry is not None and entry.lots is not None else ""
    return f" {trade.symbol} {trade.label}{lots} · #{trade.master_ticket} "


def card_subtitle(trade: Trade) -> str:
    said = f" {trade.status} "
    if trade.problems:
        said += f"· {trade.problems} failed "
    return said


def step_color(step: Step) -> str:
    if step.kind == NOTE:
        return BLUE
    if step.problem:
        return RED
    if step.outcome in (Outcome.SKIPPED, Outcome.NOT_COPIED):
        return YELLOW
    if step.outcome in (Outcome.DRY_RUN, Outcome.FOLLOWS):
        return MUTED
    return FG


def card_body(trade: Trade, now: float | None = None) -> Text:
    """One line per step, oldest first: when, and what was done on the slave."""
    out = Text()
    for n, step in enumerate(trade.steps):
        if n:
            out.append("\n")
        out.append(f"{when(step.ts, now):>12}  ", style=MUTED)
        color = step_color(step)
        if step.action:
            out.append(step.action.upper(), style=f"bold {color}")
            out.append("  ")
        out.append(step.slave, style=color if step.problem or step.kind == NOTE else FG)
        if step.master:
            out.append(f" · {step.master}", style=MUTED)
    return out


def card_class(trade: Trade) -> str:
    if trade.problems:
        return "-failed"
    return "-open" if trade.status in ("open", "resting") else ""


def shown(trades: Sequence[Trade], *, problems_only: bool) -> list[Trade]:
    return [t for t in trades if t.problems] if problems_only else list(trades)


class HistoryScreen(Screen[None]):
    """One slave's copied trades. Holds only the terminal id; the store is read every two
    seconds, which is plenty for steps a person reads after the fact."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("backspace", "app.pop_screen", "Back", show=False),
        Binding("f", "problems", "Failed only"),
    ]

    def __init__(self, terminal_id: str) -> None:
        super().__init__()
        self.terminal_id = terminal_id
        self._problems_only = False
        self._drawn: list[tuple[str, str, str, str]] = []
        """What each card was last drawn with, so an unchanged page is not rebuilt."""

    @property
    def copier(self) -> CopierApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield TopBar("", id="topbar")
        with VerticalScroll(id="hs-page"):
            yield Static("", id="hs-band")
            yield Static("", id="hs-head", classes="heading")
            yield Static("", id="hs-empty")
            yield Vertical(id="hs-cards")
        yield KeyBar("", id="keybar")

    def on_mount(self) -> None:
        self.set_interval(2.0, self._on_timer)
        self.refresh_page()
        self.query_one("#hs-page").focus()

    def _on_timer(self) -> None:
        try:
            self.refresh_page()
        except NoMatches:  # the screen is being torn down under the tick
            pass

    def refresh_page(self) -> None:
        app = self.copier
        cfg = app._cfg
        name = cfg.display_name(self.terminal_id)
        self.query_one("#topbar", TopBar).update(app.topbar(crumb=f"{name} · history"))
        band = Text(spaced("history"), style="bold")
        band.append(f"   {name}", style="bold")
        self.query_one("#hs-band", Static).update(
            left_right(band, Text(f"trades copied in the last {cfg.history_days} days"))
        )

        kept = app._engine.trade_history(self.terminal_id)
        trades = shown(kept, problems_only=self._problems_only)
        right = Text(f"{len(trades)} of {len(kept)}" if self._problems_only else str(len(kept)))
        if self._problems_only:
            right.append(" · with a failed step", style=YELLOW)
        self.query_one("#hs-head", Static).update(
            left_right(Text("TRADES COPIED", style="bold"), right)
        )

        empty = self.query_one("#hs-empty", Static)
        if not trades:
            empty.update(
                Text(
                    "No trade with a failed step."
                    if self._problems_only and kept
                    else "No trade copied to this slave yet: each one appears here when it "
                    "is copied, with every step taken with it after.",
                    style=MUTED,
                )
            )
        empty.display = not trades
        self._draw(trades[:SHOWN])

        keys = [key_hint("esc", "back"), key_hint("↑↓", "scroll")]
        keys.append(key_hint("f", "show all" if self._problems_only else "failed only"))
        keys.append(key_hint("g", "days kept"))
        self.query_one("#keybar", KeyBar).update(app.keybar(keys))

    def _draw(self, trades: Sequence[Trade]) -> None:
        now = time.time()
        wanted = [
            (card_title(t), card_subtitle(t), card_class(t), card_body(t, now).plain)
            for t in trades
        ]
        if wanted == self._drawn:
            return
        self._drawn = wanted
        holder = self.query_one("#hs-cards", Vertical)
        holder.remove_children()
        cards = []
        for trade in trades:
            card = Static(card_body(trade, now), classes=f"hs-card {card_class(trade)}")
            card.border_title = card_title(trade)
            card.border_subtitle = card_subtitle(trade)
            cards.append(card)
        holder.mount_all(cards)

    def action_problems(self) -> None:
        self._problems_only = not self._problems_only
        self.refresh_page()
