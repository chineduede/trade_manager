"""The copier's own settings, in the new look: its mode, its connection timing and its join grace,
written as sentences like a slave's settings. Opened with ``g``.

What a value means and whether it may change is :mod:`mt5copier.general_edit`'s; this lays the
fields out, reads them back and shows its messages. Going live opens
:class:`~mt5copier.tui_v2.dialogs.GoLiveDialog` before anything is saved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from ..general_edit import (
    APPLIES_ON_CONNECT,
    DRY_RUN,
    GENERAL,
    HELP,
    GeneralOutcome,
    apply_general,
    commit_general,
    initial_general,
)
from ..logging_setup import get_logger
from ..settings_edit import describe_changes
from .dialogs import GoLiveDialog
from .settings import Choice, lead, rows, section, words
from .style import FG, MUTED, RED, YELLOW, spaced

if TYPE_CHECKING:
    from .app import NewCopierApp

log = get_logger(__name__)

_F = "f-"
_E = "e-"

_MODE = (
    ("simulates every copy: dry run", "dry"),
    ("sends real orders: live", "live"),
)


class GeneralForm(ModalScreen[bool]):
    """The copier's own settings. Dismisses with True if something was saved."""

    DEFAULT_CLASSES = "form-screen"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        # A modal hides the app's own keys, and the panic key must not be one of them.
        Binding("f9", "panic", "PANIC halt"),
    ]

    @property
    def copier(self) -> NewCopierApp:
        return self.app  # type: ignore[return-value]

    # --- composition ------------------------------------------------------------------------

    def _input(self, name: str, value: str | bool, width: str) -> Input:
        return Input(value=str(value), id=f"{_F}{name}", compact=True, classes=width)

    @staticmethod
    def _errors(*names: str) -> list[Widget]:
        return [Static("", id=f"{_E}{name}", classes="err") for name in names]

    def compose(self) -> ComposeResult:
        cfg = self.copier._cfg
        v = initial_general(cfg)
        title = Text(spaced("settings"), style=f"bold {FG}")
        title.append("   the copier", style="bold")
        head = Horizontal(
            Static(title, id="st-title"),
            Static(Text("applies to every terminal", style=MUTED), id="st-where"),
            id="st-head",
        )
        with VerticalScroll(id="dialog", classes="settings", can_focus=False):
            yield head
            yield Static(self._mode_note(), id="st-mode")
            yield section(
                "mode",
                rows(
                    lead("The copier"),
                    Choice(_MODE, "dry" if v[DRY_RUN] else "live", id=f"{_F}{DRY_RUN}"),
                ),
                *self._errors(DRY_RUN),
            )
            yield section(
                "connection",
                rows(
                    lead("Check each terminal every"),
                    self._input("poll_interval_ms", v["poll_interval_ms"], "n8"),
                    words("milliseconds."),
                ),
                rows(
                    lead("Call a terminal stale after"),
                    self._input("heartbeat_timeout_s", v["heartbeat_timeout_s"], "n6"),
                    words("seconds without news."),
                ),
                rows(
                    lead("Retry a lost connection after"),
                    self._input("reconnect_backoff_s", v["reconnect_backoff_s"], "grow"),
                    words("seconds, the last repeating."),
                ),
                *self._errors("poll_interval_ms", "heartbeat_timeout_s", "reconnect_backoff_s"),
            )
            yield section(
                "joining",
                rows(
                    lead("Treat a gap of up to"),
                    self._input("join_grace_seconds", v["join_grace_seconds"], "n5"),
                    words("seconds in watching the master as unbroken."),
                ),
                *self._errors("join_grace_seconds"),
            )
            yield Static(
                Text(
                    "The check interval and the retry schedule reach a terminal the next time "
                    "its connection starts. The rest apply at once.",
                    style=MUTED,
                ),
                id="st-note",
            )
            yield Static("", id="help")
            yield Static("", id="general-error")
            with Horizontal(id="st-foot"):
                yield Static("", id="st-stake")
                yield Button("Save  ctrl+s", id="save", classes="keep")
                yield Button("Cancel  esc", id="cancel", classes="plain")

    def _mode_note(self) -> Text:
        cfg = self.copier._cfg
        out = Text()
        if cfg.dry_run:
            out.append("DRY RUN", style=f"bold {FG}")
            out.append(" · copies are only simulated now.", style=MUTED)
        else:
            out.append("LIVE COPYING", style=f"bold {RED}")
            out.append(" · orders are being sent now.", style=MUTED)
        if cfg.copying_enabled:
            out.append(
                " Copying is running: stop it with d before changing the mode.",
                style=YELLOW,
            )
        else:
            out.append(" Going live asks you to type LIVE.", style=MUTED)
        return out

    def on_mount(self) -> None:
        self.query_one(f"#{_F}{DRY_RUN}").focus()

    # --- reading back -----------------------------------------------------------------------

    def _collect(self) -> dict[str, str | bool]:
        raw: dict[str, str | bool] = {}
        for name in GENERAL:
            widget = self.query_one(f"#{_F}{name}")
            if isinstance(widget, Choice):
                raw[name] = widget.value == "dry"
            elif isinstance(widget, Input):
                raw[name] = widget.value
        return raw

    def _show_errors(self, errors: dict[str, str]) -> None:
        for name in GENERAL:
            message = errors.get(name, "")
            line = self.query_one(f"#{_E}{name}", Static)
            line.update(Text(message, style=RED) if message else "")
            line.display = bool(message)
        general = self.query_one("#general-error", Static)
        general.update(Text(errors.get("", ""), style=f"bold {RED}"))
        general.display = bool(errors.get(""))

    # --- help follows focus -----------------------------------------------------------------

    @on(events.DescendantFocus)
    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        name = (event.widget.id or "").removeprefix(_F)
        if name not in HELP:
            return
        label, text = HELP[name]
        line = Text()
        line.append(label, style=f"bold {FG}")
        line.append(f" · {text}", style=MUTED)
        self.query_one("#help", Static).update(line)

    # --- actions ----------------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_panic(self) -> None:
        self.copier.action_panic()
        self.dismiss(False)

    def action_save(self) -> None:
        app = self.copier
        outcome = apply_general(app._cfg, self._collect())
        self._show_errors(outcome.errors)
        if outcome.confirm_live:
            slaves = [app._cfg.display_name(t.terminal_id) for t in app._cfg.slaves]

            def answered(typed: str | None) -> None:
                if typed is None:
                    return
                # Checked again against the config as it is now, with the answer.
                again = apply_general(app._cfg, self._collect(), typed=typed)
                self._show_errors(again.errors)
                if again.ok:
                    self._commit(again)

            app.push_screen(GoLiveDialog(slaves, app.action_panic), answered)
            return
        if not outcome.ok:
            first = next((n for n in GENERAL if n in outcome.errors), None)
            if first is not None:
                self.query_one(f"#{_F}{first}").focus()
            return
        self._commit(outcome)

    def _commit(self, outcome: GeneralOutcome) -> None:
        app = self.copier
        if not outcome.changes:
            app.notify("the copier's settings: no changes")
            self.dismiss(False)
            return
        commit_general(app._cfg, outcome)
        log.warning("copier settings changed: %s", describe_changes(outcome.changes))
        app._persist_and_sync()
        changed = {c.name for c in outcome.changes}
        said = [f"Saved: {describe_changes(outcome.changes, limit=160)}."]
        live = DRY_RUN in changed and not app._cfg.dry_run
        if live:
            said.append("LIVE: real orders are sent once copying starts (d).")
        if changed & set(APPLIES_ON_CONNECT):
            said.append(
                "The check interval and retry schedule reach each terminal the next time its connection starts."
            )
        app.notify(" ".join(said), severity="warning" if live else "information", timeout=12)
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
