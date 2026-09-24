"""The per-terminal settings modal.

A form and nothing else. Every decision about what a value means -- parsing, bounds, what counts
as a change, what deserves a warning -- is in :mod:`mt5copier.settings_edit`, where it can be
tested without a terminal emulator. This module lays the fields out, reads them back, and shows
the messages that module returns.

The screen is handed callables rather than a config object. ``current`` returns the terminal's
*live* settings and is called at the moment of saving, so an edit is built on what is true then
and not on what was true when the form opened. ``copies`` counts what the slave has open, which
is what guards ``invert``. ``apply`` receives the validated result and returns a reason if it
was refused after all, and it is called in the same handler as the validation with nothing
awaited between them, so nothing else can change the terminal in the gap.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.markup import escape
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea

from ..config import TerminalConfig
from ..settings_edit import (
    EDITABLE,
    SECTIONS,
    EditableField,
    EditOutcome,
    Kind,
    apply_edits,
    initial_values,
)

_FIELD_PREFIX = "f-"
_ERROR_PREFIX = "e-"


def _on_off(flag: bool) -> str:
    return "on" if flag else "off"


class SettingsScreen(ModalScreen[bool]):
    """Edit one terminal's settings. Dismisses with True if something was saved."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        # A modal hides the app's own keys, and the panic key must not be one of them: copying
        # can be live while this form is open.
        Binding("f9", "panic", "PANIC halt"),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }
    SettingsScreen #dialog {
        width: 90;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        border: thick $primary;
        background: $surface;
        padding: 0 1;
    }
    SettingsScreen .section {
        color: $accent;
        margin-top: 1;
    }
    SettingsScreen .section.first {
        margin-top: 0;
    }
    SettingsScreen .row {
        height: auto;
    }
    SettingsScreen .label {
        width: 26;
    }
    SettingsScreen Input {
        width: 24;
    }
    SettingsScreen Input.wide {
        width: 46;
    }
    SettingsScreen Select {
        width: 46;
    }
    SettingsScreen TextArea {
        width: 46;
        height: 4;
    }
    SettingsScreen .error {
        display: none;
        margin-left: 26;
        color: $error;
    }
    SettingsScreen .error.flag {
        margin-left: 4;
    }
    SettingsScreen #help {
        height: auto;
        min-height: 2;
        margin-top: 1;
        color: $text-muted;
    }
    SettingsScreen #readonly {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen #general-error {
        display: none;
        height: auto;
        color: $error;
    }
    SettingsScreen #buttons {
        height: 3;
        align-horizontal: right;
    }
    """

    def __init__(
        self,
        current: Callable[[], TerminalConfig],
        copies: Callable[[], int | None],
        apply: Callable[[EditOutcome], str | None],
        halt: Callable[[], None],
        live: bool,
    ) -> None:
        super().__init__()
        self._current = current
        self._copies = copies
        self._apply = apply
        self._halt = halt
        self._live = live

    # --- composition ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        cfg = self._current()
        initial = initial_values(cfg)
        # One scroll container for the whole dialog, not a scrolling middle between fixed ends.
        # On a short terminal the buttons would otherwise be clipped off the bottom; this way
        # the dialog scrolls, and tabbing to a button brings it into view.
        #
        # Not focusable itself: a scroll container is by default, which puts the dialog frame
        # in the tab order between the real fields.
        with VerticalScroll(id="dialog", can_focus=False):
            yield Static(f"[b]Settings[/b]  {escape(cfg.label)}  ({cfg.role.value})", id="title")
            yield Static(
                "[bold red]LIVE[/] - a saved change applies to the next trade copied."
                if self._live
                else "[dim]dry run - nothing is sent either way.[/dim]",
                id="mode",
            )
            for index, section in enumerate(SECTIONS):
                yield Static(
                    f"[b]{section.title}[/b]",
                    classes="section first" if index == 0 else "section",
                )
                for spec in section.fields:
                    yield from self._row(spec, initial[spec.name])
            yield Static("", id="help")
            yield Static(
                f"[dim]Not editable here:[/dim] live trading on a non-demo account "
                f"({_on_off(cfg.allow_live_trading)}) is the demo-only guard's override, and is "
                "changed in config.json with the app closed. The terminal's name comes from "
                "discovery. Copies already open keep the size they opened at.",
                id="readonly",
            )
            yield Static("", id="general-error")
            with Horizontal(id="buttons"):
                yield Button("Save  ctrl+s", variant="primary", id="save")
                yield Button("Cancel  esc", id="cancel")

    def _row(self, spec: EditableField, value: str | bool) -> ComposeResult:
        with Horizontal(classes="row"):
            if spec.kind is not Kind.FLAG:
                yield Label(spec.label, classes="label")
            yield self._control(spec, value)
        # Under the field, not beside it: a wide field leaves no room for a message to its right,
        # and this way a message gets the whole width. Hidden until there is something to say, so
        # a clean form is no taller for having them.
        yield Static(
            "",
            id=f"{_ERROR_PREFIX}{spec.name}",
            classes="error flag" if spec.kind is Kind.FLAG else "error",
        )

    @staticmethod
    def _control(spec: EditableField, value: str | bool) -> Widget:
        widget_id = f"{_FIELD_PREFIX}{spec.name}"
        if spec.kind is Kind.FLAG:
            # The label rides inside the checkbox, so a switch takes one row and not two.
            return Checkbox(spec.label, value=bool(value), id=widget_id, compact=True)
        if spec.kind is Kind.CHOICE:
            return Select(
                spec.choices, value=str(value), allow_blank=False, id=widget_id, compact=True
            )
        if spec.kind is Kind.SYMBOL_MAP:
            # Tab leaves it rather than inserting a tab, or the form could not be tabbed through.
            return TextArea(
                str(value),
                id=widget_id,
                compact=True,
                tab_behavior="focus",
                placeholder=spec.placeholder,
            )
        return Input(
            value=str(value),
            id=widget_id,
            compact=True,
            placeholder=spec.placeholder,
            classes="wide" if spec.kind is Kind.SYMBOLS else "",
        )

    def on_mount(self) -> None:
        self.query_one(f"#{_FIELD_PREFIX}{EDITABLE[0].name}").focus()

    # --- reading back -----------------------------------------------------------------------

    def _collect(self) -> dict[str, str | bool]:
        raw: dict[str, str | bool] = {}
        for spec in EDITABLE:
            widget: Widget = self.query_one(f"#{_FIELD_PREFIX}{spec.name}")
            if isinstance(widget, Input):
                raw[spec.name] = widget.value
            elif isinstance(widget, TextArea):
                raw[spec.name] = widget.text
            elif isinstance(widget, Select):
                raw[spec.name] = str(widget.value)
            elif isinstance(widget, Checkbox):
                raw[spec.name] = bool(widget.value)
        return raw

    def _show_errors(self, errors: dict[str, str]) -> None:
        for spec in EDITABLE:
            message = errors.get(spec.name, "")
            line = self.query_one(f"#{_ERROR_PREFIX}{spec.name}", Static)
            line.update(f"[red]{escape(message)}[/red]" if message else "")
            line.display = bool(message)
        self._show_general(errors.get(""))

    def _show_general(self, message: str | None) -> None:
        line = self.query_one("#general-error", Static)
        line.update(f"[bold red]{escape(message)}[/]" if message else "")
        line.display = bool(message)

    # --- help follows focus, so the form stays one row per field ----------------------------

    @on(events.DescendantFocus)
    def _on_focus(self, event: events.DescendantFocus) -> None:
        widget_id = event.widget.id or ""
        if not widget_id.startswith(_FIELD_PREFIX):
            return
        name = widget_id.removeprefix(_FIELD_PREFIX)
        spec = next((s for s in EDITABLE if s.name == name), None)
        if spec is not None:
            self.query_one("#help", Static).update(escape(spec.help))

    # --- actions ----------------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_panic(self) -> None:
        """Halt copying, then leave the form so the app's real state is what is on screen.

        Anything typed and unsaved is dropped. That is the right trade in an emergency stop: a
        form left open over a halted copier says nothing about being halted.
        """
        self._halt()
        self.dismiss(False)

    def action_save(self) -> None:
        outcome = apply_edits(self._current(), self._collect(), open_copies=self._copies())
        self._show_errors(outcome.errors)
        if not outcome.ok:
            first = next((s for s in EDITABLE if s.name in outcome.errors), None)
            if first is not None:
                self.query_one(f"#{_FIELD_PREFIX}{first.name}").focus()
            return
        refusal = self._apply(outcome)
        if refusal is not None:
            # The engine checks again under its own lock, so it can refuse something the form
            # let through a moment earlier. The form stays open and says so.
            self._show_general(refusal)
            return
        self.dismiss(True)

    @on(Button.Pressed, "#save")
    def _on_save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.action_cancel()

    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save()
