"""The confirmation shown before a change gives up open copies: a settings edit, or a role
being taken away.

Every change is treated alike: one that stops the copier managing open copies is only made once
the person has been shown what they are giving up and agrees. Which edits *would* have been safe
under an open copy is a question nobody has to answer. This screen is that showing.

Unassigning the master strands every slave's copies at once, so the copies are listed per slave.

It defaults to Cancel. Confirming abandons real positions -- they stay open, and nothing closes
or moves them any more -- so the resting choice is the one that changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..reconcile import OpenCopy
from ..settings_edit import copies_word, describe_copy

_LISTED = 8
"""Copies named on screen. A long list would push the buttons off a small terminal; the log
carries every one, and the count in the title is always the whole number."""


class OrphanConfirmScreen(ModalScreen[bool]):
    """Ask whether to give up open copies. Dismisses with True if agreed.

    ``copies`` is keyed by the name of the slave that holds them. ``unassigning`` words the
    screen for a role being taken away rather than a settings edit.
    """

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
        copies: Mapping[str, Sequence[OpenCopy]],
        changes: str,
        live: bool,
        halt: Callable[[], None],
        *,
        unassigning: bool = False,
    ) -> None:
        super().__init__()
        self._copies = {name: list(held) for name, held in copies.items() if held}
        self._changes = changes
        self._live = live
        self._halt = halt
        self._unassigning = unassigning

    def compose(self) -> ComposeResult:
        total = sum(len(held) for held in self._copies.values())
        grouped = len(self._copies) > 1
        lines: list[str] = []
        shown = 0
        for name, held in self._copies.items():
            if grouped:
                lines.append(f"[b]{escape(name)}[/b]")
            for copy in held:
                if shown == _LISTED:
                    break
                lines.append(("  " if grouped else "") + escape(describe_copy(copy)))
                shown += 1
        if total > shown:
            lines.append(f"... and {total - shown} more (all are in the log)")
        holders = ", ".join(self._copies)
        with VerticalScroll(id="dialog", can_focus=False):
            yield Static(
                f"[b]Orphan {copies_word(total)} on {escape(holders)}?[/b]",
                id="title",
            )
            if not self._live:
                yield Static(
                    "[dim]Dry run: these copies are simulated. Nothing real is open, and "
                    "nothing real is affected.[/dim]",
                    id="dryrun",
                )
            yield Static(
                (
                    "Unassigning stops the copier managing what is open on the slaves it affects:"
                    if self._unassigning
                    else "Changing these settings stops the copier managing what this terminal "
                    "has open:"
                ),
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
                f"[dim]The change:[/dim] {escape(self._changes)}."
                + ("" if self._unassigning else " It applies to every trade copied from now on."),
                id="changes",
            )
            with Horizontal(id="buttons"):
                yield Button(
                    "Orphan them and unassign" if self._unassigning else "Orphan them and apply",
                    variant="error",
                    id="go",
                )
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
