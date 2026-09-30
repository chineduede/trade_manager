"""The confirmation shown before a settings change gives up a terminal's open copies.

Every setting is treated alike: an edit to a terminal that holds copies stops the copier managing
them. Which edits *would* have been safe under an open copy is a question nobody has to answer,
so long as the person is shown what they are giving up and agrees. This screen is that showing.

It defaults to Cancel. Confirming abandons real positions -- they stay open, and nothing closes
or moves them any more -- so the resting choice is the one that changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..reconcile import OpenCopy
from ..settings_edit import copies_word

_LISTED = 8
"""Copies named on screen. A long list would push the buttons off a small terminal; the log
carries every one, and the count in the title is always the whole number."""


def describe_copy(copy: OpenCopy) -> str:
    ticket = f"#{copy.slave_ticket}" if copy.slave_ticket is not None else "(no ticket)"
    kind = "  resting order" if copy.resting else ""
    volume = f"{copy.volume:g} lots" if copy.volume is not None else "lots unknown"
    return f"{ticket}  {copy.symbol}  {volume}{kind}"


class OrphanConfirmScreen(ModalScreen[bool]):
    """Ask whether to give up a terminal's open copies. Dismisses with True if agreed."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        # A modal hides the app's own keys, and the panic key must not be one of them.
        Binding("f9", "panic", "PANIC halt"),
    ]

    DEFAULT_CSS = """
    OrphanConfirmScreen {
        align: center middle;
    }
    OrphanConfirmScreen #dialog {
        width: 78;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        border: thick $error;
        background: $surface;
        padding: 0 1;
    }
    OrphanConfirmScreen Static {
        margin-top: 1;
    }
    OrphanConfirmScreen #title {
        margin-top: 0;
    }
    OrphanConfirmScreen #copies {
        margin-top: 0;
    }
    OrphanConfirmScreen #buttons {
        height: 3;
        margin-top: 1;
        align-horizontal: right;
    }
    """

    def __init__(
        self,
        label: str,
        copies: Sequence[OpenCopy],
        changes: str,
        live: bool,
        halt: Callable[[], None],
    ) -> None:
        super().__init__()
        self._label = label
        self._copies = list(copies)
        self._changes = changes
        self._live = live
        self._halt = halt

    def compose(self) -> ComposeResult:
        shown = self._copies[:_LISTED]
        lines = [escape(describe_copy(c)) for c in shown]
        if len(self._copies) > len(shown):
            lines.append(f"... and {len(self._copies) - len(shown)} more (all are in the log)")
        with VerticalScroll(id="dialog", can_focus=False):
            yield Static(
                f"[b]Orphan {copies_word(len(self._copies))} on {escape(self._label)}?[/b]",
                id="title",
            )
            if not self._live:
                yield Static(
                    "[dim]Dry run: these copies are simulated. Nothing real is open, and "
                    "nothing real is affected.[/dim]",
                    id="dryrun",
                )
            yield Static(
                "Changing these settings stops the copier managing what this terminal has open:",
                id="lead",
            )
            yield Static("\n".join(lines), id="copies")
            yield Static(
                "[bold]They stay open on the slave.[/bold] The copier will no longer close them "
                "when the master does, move their stops or targets, or mirror partial closes: "
                "manage them by hand. They are listed as untracked positions of ours, and the "
                "trades the master still holds for them are not copied again.",
                id="consequence",
            )
            yield Static(
                f"[dim]The change:[/dim] {escape(self._changes)}. It applies to every trade "
                "copied from now on.",
                id="changes",
            )
            with Horizontal(id="buttons"):
                yield Button("Orphan them and apply", variant="error", id="go")
                yield Button("Cancel  esc", id="cancel")

    def on_mount(self) -> None:
        # Cancel is where focus rests: confirming has to be a deliberate move off it.
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
