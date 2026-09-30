"""The modal that turns the demo-only guard's live-trading override on or off.

Its own screen and its own key, not a row in the settings form. That form is saved with ``ctrl+s``
or Enter, and a guard that a stray keypress on a checkbox can lift is not a guard.

The two directions are deliberately unequal. **On** is the dangerous one: the copier will send
real orders to an account it cannot tell is a demo, so it asks for the terminal's name typed out
-- and the name, not a fixed word, because the mistake being guarded against is the wrong
terminal. **Off** is the safe direction and takes one press. What each direction *means* -- who
may turn it on, what has to be typed -- is decided in :func:`mt5copier.settings_edit.set_live_override`
so that it can be tested without a terminal emulator; this module lays out the dialog and shows
what that function says.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..config import TerminalConfig
from ..settings_edit import EditOutcome, live_override_phrase, set_live_override


class LiveOverrideScreen(ModalScreen[bool]):
    """Turn one terminal's live-trading override on or off. Dismisses with True if it changed."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        # A modal hides the app's own keys, and the panic key must not be one of them.
        Binding("f9", "panic", "PANIC halt"),
    ]

    DEFAULT_CSS = """
    LiveOverrideScreen {
        align: center middle;
    }
    LiveOverrideScreen #dialog {
        width: 76;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        border: thick $error;
        background: $surface;
        padding: 0 1;
    }
    LiveOverrideScreen Static {
        margin-top: 1;
    }
    LiveOverrideScreen #title {
        margin-top: 0;
    }
    LiveOverrideScreen Input {
        width: 100%;
    }
    LiveOverrideScreen #error {
        display: none;
        height: auto;
        color: $error;
    }
    LiveOverrideScreen #buttons {
        height: 3;
        margin-top: 1;
        align-horizontal: right;
    }
    """

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

    # --- composition ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        cfg = self._current()
        with VerticalScroll(id="dialog", can_focus=False):
            yield Static(
                f"[b]Live trading override[/b]  {escape(cfg.label)}  ({cfg.role.value})",
                id="title",
            )
            if cfg.allow_live_trading:
                yield Static(
                    "[bold red]ON[/] - this terminal is traded whatever its account is, a real "
                    "or prop-firm account included.",
                    id="state",
                )
                yield Static(
                    "Turning it off puts the demo-only guard back. Like any settings change, it "
                    "gives up copies already open on this terminal, and you are asked first.",
                    id="explain",
                )
            else:
                yield Static(
                    "[b]OFF[/b] - this terminal is traded only while its account is a demo. A "
                    "real or prop-firm account, or one whose type cannot be read, is refused.",
                    id="state",
                )
                yield Static(
                    "Turning it on lets the copier send real orders to this terminal whatever "
                    "its account is: opens, closes, and stop or target moves. Trades the master "
                    "opened while it was off stay declined. Like any settings change, it gives "
                    "up copies already open on this terminal, and you are asked first.",
                    id="explain",
                )
                yield Static(
                    f"Type the terminal's name to turn it on: [b]{escape(live_override_phrase(cfg))}[/b]",
                    id="prompt",
                )
                yield Input(placeholder="the terminal's name", id="typed", compact=True)
            yield Static("", id="error")
            with Horizontal(id="buttons"):
                yield Button(
                    "Turn OFF" if cfg.allow_live_trading else "Turn ON",
                    variant="primary" if cfg.allow_live_trading else "error",
                    id="go",
                )
                yield Button("Cancel  esc", id="cancel")

    def on_mount(self) -> None:
        # Turning it on starts in the text box. Turning it off has no box, and the button is the
        # safe direction, so it is where focus rests.
        box = self.query("#typed")
        (box.first() if box else self.query_one("#go")).focus()

    # --- actions ----------------------------------------------------------------------------

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
