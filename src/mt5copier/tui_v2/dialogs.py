"""The two questions that guard money, in the new look: giving up open copies (unassigning, or
a settings change), and lifting the demo-only guard.

Each is the classic dialog with its drawing replaced. What is asked, what has to be typed, and
what is done with the answer all stay the classic screen's -- the buttons, the text box and the
error line keep their ids, so its handlers find them -- and only ``compose`` is new here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Static

from ..config import Role, SizingMode, TerminalConfig
from ..protocol import ACCOUNT_TRADE_MODE_DEMO, PositionType, Snapshot
from ..reconcile import OpenCopy
from ..settings_edit import live_override_phrase
from ..tui.live_override_screen import LiveOverrideScreen
from ..tui.orphan_screen import _LISTED, OrphanConfirmScreen
from .style import FG, MUTED, RED, RULED, amount

if TYPE_CHECKING:
    from .app import NewCopierApp


def _count_word(n: int) -> str:
    words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}
    return words.get(n, str(n))


def copy_line(copy: OpenCopy, side: str | None) -> str:
    """``BTCUSD buy 3.11 lots``, or a resting order's like it."""
    lots = f"{copy.volume:g} lots" if copy.volume is not None else "lots unknown"
    what = " ".join(p for p in (copy.symbol, side, lots) if p)
    return what + (", resting order" if copy.resting else "")


def unassign_lead(role: Role, slaves: int, holders: int) -> str:
    """What unassigning takes with it, before the copies are listed."""
    if role is Role.MASTER and slaves:
        released = f"its {slaves} slave{'s are' if slaves != 1 else ' is'} released too"
        if holders == 1 and slaves == 1:
            held = "It holds open copies:"
        elif holders == 1:
            held = "One of them holds open copies:"
        else:
            held = f"{_count_word(holders)} of them hold open copies:"
        return f"It is the master, so {released}. {held}"
    return "It holds open copies:"


def copies_table(rows: Sequence[tuple[str, str, str]]) -> Table:
    """Slave / Open copy / Ticket, ruled between rows as the design has it."""
    table = Table(
        box=RULED,
        show_edge=False,
        show_lines=True,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        border_style=MUTED,
        header_style=f"bold {FG}",
        expand=True,
    )
    table.add_column("Slave", no_wrap=True, ratio=1)
    table.add_column("Open copy", no_wrap=True, ratio=1)
    table.add_column("Ticket", justify="right", no_wrap=True, style=MUTED)
    for row in rows:
        table.add_row(*row)
    return table


class OrphanDialog(OrphanConfirmScreen):
    """Before open copies are given up. Esc, and the button that rests focused, keep them."""

    @property
    def copier(self) -> NewCopierApp:
        return self.app  # type: ignore[return-value]

    def _sides(self) -> dict[int, str]:
        """Each open position's side, by ticket, read off the slaves' snapshots."""
        sides: dict[int, str] = {}
        for v in self.copier._views():
            for p in (v.snapshot.open_positions if v.snapshot else None) or ():
                sides[p.ticket] = "buy" if p.type is PositionType.BUY else "sell"
        return sides

    def compose(self) -> ComposeResult:
        app = self.copier
        cfg = app._cfg
        tid = app._selected_id()
        name = cfg.display_name(tid) if tid else ", ".join(self._copies)
        numbers = app._terminal_numbers()
        by_name = {cfg.display_name(t): t for t in cfg.terminals}
        sides = self._sides()

        total = sum(len(held) for held in self._copies.values())
        rows: list[tuple[str, str, str]] = []
        for holder, held in self._copies.items():
            number = numbers.get(by_name.get(holder, ""), "")
            for copy in held:
                if len(rows) == _LISTED:
                    break
                ticket = f"#{copy.slave_ticket}" if copy.slave_ticket is not None else "—"
                side = sides.get(copy.slave_ticket) if copy.slave_ticket is not None else None
                rows.append((f"{number} {holder}".strip(), copy_line(copy, side), ticket))

        with VerticalScroll(id="dialog", classes="dialog", can_focus=False):
            if self._unassigning:
                role = cfg.terminals[tid].role if tid else Role.SLAVE
                title = f"Unassign {name}?"
                lead = unassign_lead(role, len(cfg.slaves), len(self._copies))
                go = f"Unassign and orphan {total} cop{'y' if total == 1 else 'ies'}"
            else:
                title = f"Change {name}'s settings?"
                lead = "Changing these settings stops the copier managing what it has open:"
                go = f"Apply and orphan {total} cop{'y' if total == 1 else 'ies'}"
            yield Static(Text(title.upper(), style="bold"), classes="dialog-title")
            if not self._live:
                yield Static(
                    Text(
                        "Dry run: these copies are simulated. Nothing real is open, and nothing "
                        "real is affected.",
                        style=MUTED,
                    )
                )
            yield Static(lead)
            more = Text()
            if total > len(rows):
                more = Text(f"and {total - len(rows)} more, all named in the log", style=MUTED)
            yield Static(copies_table(rows), id="copies")
            if more.plain:
                yield Static(more)
            said = Text()
            said.append("They stay open.", style=f"bold {RED}")
            said.append(" The copier stops managing them:\n")
            for line in (
                "not closed when the master closes",
                "no stop or target moves, no partial closes",
                "the master's trade is not copied again",
            ):
                said.append(f"  · {line}\n")
            said.append(
                "You manage them by hand from here. They are listed as untracked positions.",
                style=MUTED,
            )
            yield Static(said, id="consequence")
            if not self._unassigning:
                change = Text("The change: ", style=MUTED)
                change.append(f"{self._changes}. It applies to every trade copied from now on.")
                yield Static(change, id="changes")
            with Horizontal(id="buttons"):
                yield Button("Keep everything  esc", id="cancel", classes="keep")
                yield Button(go, id="go", classes="danger")


# --- the demo-only guard -------------------------------------------------------------------------


def account_kind(snap: Snapshot | None) -> Text:
    if snap is None or snap.trade_mode is None:
        return Text("could not be read, so it counts as real money", style=f"bold {RED}")
    if snap.trade_mode == ACCOUNT_TRADE_MODE_DEMO:
        return Text("demo")
    return Text("not a demo: real money", style=f"bold {RED}")


def will_copy(cfg: TerminalConfig, master: str | None) -> str:
    """``A IC Markets Global, risking 1% of balance per trade``."""
    if cfg.role is not Role.SLAVE:
        return "nothing: it is not a slave"
    if cfg.sizing_mode is SizingMode.FIXED_CASH_RISK:
        risk = f"risking {cfg.risk_cash:,g} per trade"
    else:
        risk = f"risking {cfg.risk_percent:g}% of balance per trade"
    return f"A {master}, {risk}" if master else f"the master, once there is one, {risk}"


def letters_to_go(typed: str, phrase: str) -> Text:
    """How far the typing has got: ``3 letters to go``, or that it has gone wrong."""
    have = typed.strip().casefold()
    want = phrase.casefold()
    if have == want:
        return Text("that is the name", style=f"bold {FG}")
    if not want.startswith(have):
        return Text("not the name", style=f"bold {RED}")
    left = len(want) - len(have)
    return Text(f"{left} letter{'s' if left != 1 else ''} to go", style=MUTED)


class LiveDialog(LiveOverrideScreen):
    """Lifting, or putting back, the demo-only guard on one terminal. On needs its name typed;
    off is one press."""

    @property
    def copier(self) -> NewCopierApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        app = self.copier
        cfg = self._current()
        name = app._cfg.display_name(cfg.terminal_id)
        view = next((v for v in app._views() if v.cfg.terminal_id == cfg.terminal_id), None)
        snap = view.snapshot if view else None
        master = app._cfg.master
        master_name = app._cfg.display_name(master.terminal_id) if master else None
        on = cfg.allow_live_trading

        login = snap.login if snap and snap.login else cfg.last_login
        server = snap.server if snap and snap.server else cfg.last_server
        facts = Table(
            box=RULED,
            show_header=False,
            show_edge=False,
            show_lines=True,
            expand=True,
            pad_edge=False,
            padding=(0, 2, 0, 0),
            border_style=MUTED,
        )
        facts.add_column(style=MUTED, no_wrap=True, width=12)
        facts.add_column(ratio=1)
        account = " · ".join(str(x) for x in (login, server) if x) or "not read yet"
        facts.add_row("Account", account)
        facts.add_row("Kind", account_kind(snap))
        facts.add_row("Balance", amount(snap.balance if snap else None))
        facts.add_row("Will copy", will_copy(cfg, master_name))

        with VerticalScroll(id="dialog", classes="dialog", can_focus=False):
            if on:
                title = f"Turn off live trading on {name}?"
            else:
                title = f"Allow live trading on {name}?"
            yield Static(Text(title.upper(), style="bold"), classes="dialog-title")
            yield Static(facts)
            if on:
                yield Static(
                    "It is traded now whatever its account is, a real or prop-firm account "
                    "included. Turning it off puts the demo-only guard back: only a demo is "
                    "traded. Like any settings change, it gives up copies already open on this "
                    "terminal, and you are asked first."
                )
            else:
                yield Static(
                    "Once allowed, every trade the master opens is sent to this account as a real "
                    "order: opens, closes, and stop or target moves. Trades the master opened "
                    "while it was held back are not copied late. Like any settings change, it "
                    "gives up copies already open on this terminal, and you are asked first."
                )
                phrase = live_override_phrase(cfg)
                prompt = Text("Type the terminal's name in full to turn it on: ", style="bold")
                prompt.append(phrase, style=f"bold {RED}")
                yield Static(prompt, id="prompt")
                with Horizontal(id="typed-box"):
                    yield Input(placeholder=phrase, id="typed", compact=True)
                    yield Static(letters_to_go("", phrase), id="to-go")
                yield Static(
                    Text(
                        "Turning it off again takes one press of l. While any terminal has it "
                        "on, the top bar shows NON-DEMO ALLOWED.",
                        style=MUTED,
                    )
                )
            yield Static("", id="error")
            with Horizontal(id="buttons"):
                if on:
                    yield Button("Keep it on  esc", id="cancel", classes="keep")
                    yield Button("Turn it off  enter", id="go", classes="keep")
                else:
                    yield Button("Keep it held back  esc", id="cancel", classes="keep")
                    yield Button("Allow live trading  enter", id="go", classes="danger -waiting")

    @on(Input.Changed, "#typed")
    def _on_typed(self, event: Input.Changed) -> None:
        phrase = live_override_phrase(self._current())
        self.query_one("#to-go", Static).update(letters_to_go(event.value, phrase))
        matches = event.value.strip().casefold() == phrase.casefold()
        self.query_one("#go", Button).set_class(not matches, "-waiting")
        self._show_error("")
