"""The questions that guard money: giving up open copies (unassigning, or a settings change),
lifting the demo-only guard, and switching the copier from a dry run to live.

Giving up copies: every change is treated alike. One that stops the copier managing open
copies is only made once the person has been shown what they are giving up and agrees. Which
edits *would* have been safe under an open copy is a question nobody has to answer. It rests
on keeping everything: confirming abandons real positions -- they stay open, and nothing closes
or moves them any more -- so the resting choice is the one that changes nothing.

The demo-only guard has its own key and its own dialog, not a row in the settings form: that
form is saved with ``ctrl+s`` or Enter, and a guard that a stray keypress on a checkbox can lift
is not a guard. The two directions are deliberately unequal. **On** is the dangerous one, and
asks for the terminal's name typed out -- the name, not a fixed word, because the mistake being
guarded against is the wrong terminal. **Off** takes one press. What each direction *means* is
decided in :func:`mt5copier.settings_edit.set_live_override`, where it can be tested without a
terminal emulator; this module lays out the dialog and shows what that function says.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..config import Role, SizingMode, TerminalConfig
from ..general_edit import LIVE_WORD
from ..protocol import ACCOUNT_TRADE_MODE_DEMO, PositionType, Snapshot
from ..reconcile import OpenCopy
from ..settings_edit import EditOutcome, live_override_phrase, set_live_override
from .style import FG, MUTED, RED, RULED, amount
from .widgets import ButtonRow

if TYPE_CHECKING:
    from .app import CopierApp

_LISTED = 8
"""Copies named on screen. A long list would push the buttons off a small terminal; the log
carries every one, and the count in the title is always the whole number."""


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


class OrphanDialog(ModalScreen[bool]):
    """Before open copies are given up. Esc, and the button that rests focused, keep them.
    Dismisses with True if agreed.

    ``copies`` is keyed by the name of the slave that holds them. ``unassigning`` words the
    screen for a role being taken away rather than a settings edit, and ``loading`` -- a saved
    setup's name -- for that setup being loaded.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        # A modal hides the app's own keys, and the panic key must not be one of them.
        Binding("f9", "panic", "PANIC halt"),
    ]

    def __init__(
        self,
        copies: Mapping[str, Sequence[OpenCopy]],
        changes: str,
        live: bool,
        halt: Callable[[], None],
        *,
        unassigning: bool = False,
        loading: str | None = None,
    ) -> None:
        super().__init__()
        self._copies = {name: list(held) for name, held in copies.items() if held}
        self._changes = changes
        self._live = live
        self._halt = halt
        self._unassigning = unassigning
        self._loading = loading

    @property
    def copier(self) -> CopierApp:
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
            if self._loading is not None:
                title = f"Load setup {self._loading}?"
                lead = "Loading it takes these slaves off what they copied from their master:"
                go = f"Load and orphan {total} cop{'y' if total == 1 else 'ies'}"
            elif self._unassigning:
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
                if self._loading is not None:
                    change.append(f"{self._changes}.")
                else:
                    change.append(f"{self._changes}. It applies to every trade copied from now on.")
                yield Static(change, id="changes")
            with ButtonRow(id="buttons"):
                yield Button("Keep everything  esc", id="cancel", classes="keep")
                yield Button(go, id="go", classes="danger")

    def on_mount(self) -> None:
        # Keeping everything is where focus rests: giving up has to be a deliberate move off it.
        self.query_one("#cancel").focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_panic(self) -> None:
        """Halt copying, and leave without agreeing to anything."""
        self._halt()
        self.dismiss(False)

    @on(Button.Pressed, "#go")
    def _on_go(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(False)


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


class LiveDialog(ModalScreen[bool]):
    """Lifting, or putting back, the demo-only guard on one terminal. On needs its name typed;
    off is one press. Dismisses with True if it changed."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        # A modal hides the app's own keys, and the panic key must not be one of them.
        Binding("f9", "panic", "PANIC halt"),
    ]

    def __init__(
        self,
        current: Callable[[], TerminalConfig],
        apply: Callable[[EditOutcome, Callable[[str | None], None]], None],
        halt: Callable[[], None],
    ) -> None:
        super().__init__()
        self._current = current
        self._apply = apply
        self._halt = halt

    @property
    def copier(self) -> CopierApp:
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
            with ButtonRow(id="buttons"):
                if on:
                    yield Button("Keep it on  esc", id="cancel", classes="keep")
                    yield Button("Turn it off  enter", id="go", classes="keep")
                else:
                    yield Button("Keep it held back  esc", id="cancel", classes="keep")
                    yield Button("Allow live trading  enter", id="go", classes="danger -waiting")

    def on_mount(self) -> None:
        # Turning it on starts in the text box. Turning it off has no box, and the button is the
        # safe direction, so it is where focus rests.
        box = self.query("#typed")
        (box.first() if box else self.query_one("#go")).focus()

    @on(Input.Changed, "#typed")
    def _on_typed(self, event: Input.Changed) -> None:
        phrase = live_override_phrase(self._current())
        self.query_one("#to-go", Static).update(letters_to_go(event.value, phrase))
        matches = event.value.strip().casefold() == phrase.casefold()
        self.query_one("#go", Button).set_class(not matches, "-waiting")
        self._show_error("")

    def _show_error(self, message: str) -> None:
        line = self.query_one("#error", Static)
        line.update(f"[bold red]{escape(message)}[/]" if message else "")
        line.display = bool(message)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_panic(self) -> None:
        """Halt copying, then leave the dialog so the app's real state is what is on screen."""
        self._halt()
        self.dismiss(False)

    def action_go(self) -> None:
        # Read at the moment of pressing, not at compose time: the direction is whatever is true
        # now, and the typed text is only asked for -- and only looked at -- when turning it on.
        current = self._current()
        enabled = not current.allow_live_trading
        box = self.query("#typed")
        typed = (box.first(Input).value if box else "") if enabled else None
        outcome = set_live_override(current, enabled, typed=typed)
        if not outcome.ok:
            self._show_error("; ".join(outcome.errors.values()))
            return
        self._apply(outcome, self._applied)

    def _applied(self, refusal: str | None) -> None:
        # Told later, not returned: a terminal that holds copies is asked about first, and if the
        # person declines this is never called and the dialog is simply still there.
        if refusal is not None:
            self._show_error(refusal)
            return
        self.dismiss(True)

    @on(Button.Pressed, "#go")
    def _on_go(self) -> None:
        self.action_go()

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.action_cancel()

    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_go()


# --- the copier going live -----------------------------------------------------------------------


class GoLiveDialog(ModalScreen[str | None]):
    """Before the copier is switched from a dry run to sending real orders.

    Dismisses with what was typed when it is ``LIVE``, which the form then saves with, or None
    when the person backs out. Copying is stopped by the time this opens -- the form refuses a
    change of mode otherwise -- so nothing is sent until ``d`` is pressed.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Keep the dry run"),
        Binding("f9", "panic", "PANIC halt"),
    ]

    def __init__(self, slaves: Sequence[str], halt: Callable[[], None]) -> None:
        super().__init__()
        self._slaves = list(slaves)
        self._halt = halt

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog", classes="dialog", can_focus=False):
            yield Static(Text("SEND REAL ORDERS?", style="bold"), classes="dialog-title")
            if self._slaves:
                who = ", ".join(self._slaves)
                to = f"to each connected slave ({who})"
            else:
                to = "to each slave, once there are some"
            yield Static(
                f"Live, every trade the master opens is sent as a real order {to}: opens, "
                "closes, and stop or target moves. A slave on a real-money account stays held "
                "back unless live trading is allowed on it (l)."
            )
            yield Static(
                Text(
                    "Copying is stopped, and stays stopped: nothing is sent until you press d. "
                    "Going back to a dry run is one choice in these settings.",
                    style=MUTED,
                )
            )
            prompt = Text("Type ", style="bold")
            prompt.append(LIVE_WORD, style=f"bold {RED}")
            prompt.append(" to switch:", style="bold")
            yield Static(prompt, id="prompt")
            with Horizontal(id="typed-box"):
                yield Input(placeholder=LIVE_WORD, id="typed", compact=True)
                yield Static(letters_to_go("", LIVE_WORD), id="to-go")
            yield Static("", id="error")
            with ButtonRow(id="buttons"):
                yield Button("Keep the dry run  esc", id="cancel", classes="keep")
                yield Button("Go live  enter", id="go", classes="danger -waiting")

    def on_mount(self) -> None:
        self.query_one("#typed", Input).focus()

    def _typed(self) -> str:
        return self.query_one("#typed", Input).value

    @on(Input.Changed, "#typed")
    def _on_typed(self, event: Input.Changed) -> None:
        self.query_one("#to-go", Static).update(letters_to_go(event.value, LIVE_WORD))
        matches = event.value.strip().upper() == LIVE_WORD
        self.query_one("#go", Button).set_class(not matches, "-waiting")
        self.query_one("#error", Static).update("")

    def _go(self) -> None:
        if self._typed().strip().upper() != LIVE_WORD:
            self.query_one("#error", Static).update(
                Text(f"Type {LIVE_WORD} in full to go live.", style=RED)
            )
            return
        self.dismiss(self._typed())

    @on(Input.Submitted, "#typed")
    def _on_submit(self) -> None:
        self._go()

    @on(Button.Pressed, "#go")
    def _on_go(self) -> None:
        self._go()

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_panic(self) -> None:
        self._halt()
        self.dismiss(None)
