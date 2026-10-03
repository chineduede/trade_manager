"""The modal that sets one copy's breakeven price, from the detail page (``B`` on a copy).

A price typed here replaces the slave's own R trigger for that one copy, and works whether
or not the slave has breakeven on. Blank clears it. Whether the price is usable -- past entry
plus commission and slippage, on the profitable side -- is the engine's call
(:meth:`mt5copier.engine.Engine.set_breakeven_at`); this only lays out the box and shows what
it says.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class BreakevenPriceScreen(ModalScreen[bool]):
    """Set or clear one copy's breakeven price. Dismisses with True if it changed."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        # A modal hides the app's own keys, and the panic key must not be one of them.
        Binding("f9", "panic", "PANIC halt"),
    ]

    DEFAULT_CSS = """
    BreakevenPriceScreen {
        align: center middle;
    }
    BreakevenPriceScreen #dialog {
        width: 64;
        max-width: 100%;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 0 1;
    }
    BreakevenPriceScreen Static {
        margin-top: 1;
    }
    BreakevenPriceScreen #title {
        margin-top: 0;
    }
    BreakevenPriceScreen Input {
        width: 100%;
    }
    BreakevenPriceScreen #error {
        display: none;
        height: auto;
        color: $error;
    }
    BreakevenPriceScreen #buttons {
        height: 3;
        margin-top: 1;
        align-horizontal: right;
    }
    """

    def __init__(
        self,
        title: str,
        current: str,
        apply: Callable[[float], str | None],
        halt: Callable[[], None],
    ) -> None:
        super().__init__()
        self._title = title
        self._current = current
        self._apply = apply
        self._halt = halt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"[b]Breakeven price[/b]  {escape(self._title)}", id="title")
            yield Static(
                "When this copy's price reaches it, its stop moves past entry by the slave's "
                "commission and slippage buffer. Replaces the slave's own trigger for this copy only, and works even "
                "with breakeven off. Blank clears it.\n\nOnce it fires the stop only tightens: "
                "the master's stop is copied only when tighter, a looser or removed one is "
                "ignored. The target still follows the master."
            )
            yield Input(value=self._current, placeholder="price, or blank to clear", id="price")
            yield Static("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Set", variant="primary", id="go")
                yield Button("Cancel  esc", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#price", Input).focus()

    def _show_error(self, message: str) -> None:
        line = self.query_one("#error", Static)
        line.update(f"[bold red]{escape(message)}[/]" if message else "")
        line.display = bool(message)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_panic(self) -> None:
        self._halt()
        self.dismiss(False)

    def action_go(self) -> None:
        text = self.query_one("#price", Input).value.strip().replace(",", "")
        if not text:
            price = 0.0
        else:
            try:
                price = float(text)
            except ValueError:
                self._show_error("that is not a price")
                return
            if not price > 0 or price == float("inf"):
                self._show_error("the price has to be above zero")
                return
        refusal = self._apply(price)
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
