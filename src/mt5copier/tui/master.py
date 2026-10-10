"""The master's settings: who manages copies once they are open, its own breakeven, and the
prefix and suffix its broker puts on symbol names.

Everything else about a copy is set on each slave. The decoration is the exception because it
describes the master's broker, and every slave needs it taken off before its own renames,
filters and decoration apply -- see :func:`mt5copier.symbols.undecorate`. Who manages open
trades is the master's half of a two-gate switch, the other half on each slave (see
:meth:`mt5copier.config.AppConfig.manages`). Its breakeven is the one thing that makes the copier
trade the master, so it is off until turned on here, and says so. Written like a slave's
settings and saved the same way, through :func:`~mt5copier.settings_edit.apply_edits`.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..config import BreakevenMode, TerminalConfig
from ..protocol import ACCOUNT_TRADE_MODE_DEMO
from ..settings_edit import BY_NAME, EditOutcome, apply_edits, initial_text
from ..symbols import undecorate
from .settings import _BREAKEVEN, Choice, lead, part, rows, section, words
from .style import FG, MUTED, RED, spaced

_F = "f-"
_E = "e-"
FIELDS = ("symbol_prefix", "symbol_suffix", "breakeven_trigger_r", "breakeven_book_percent")
CHOICES = ("management", "breakeven_mode")
_SYMBOL_FIELDS = ("symbol_prefix", "symbol_suffix")
_SHOWN = 4
"""How many of the master's symbols the example shows."""


class MasterForm(ModalScreen[bool]):
    """The master's settings. Dismisses with True if something was saved."""

    DEFAULT_CLASSES = "form-screen"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        # A modal hides the app's own keys, and the panic key must not be one of them.
        Binding("f9", "panic", "PANIC halt"),
    ]

    def __init__(
        self,
        current: Callable[[], TerminalConfig],
        apply: Callable[[EditOutcome, Callable[[str | None], None]], None],
        halt: Callable[[], None],
        name: str,
        symbols: list[str],
    ) -> None:
        super().__init__()
        self._current = current
        self._apply = apply
        self._halt = halt
        self._name = name
        self._symbols = symbols

    # --- composition ------------------------------------------------------------------------

    def _input(self, field: str, cfg: TerminalConfig, width: str = "n6") -> Input:
        return Input(
            value=str(initial_text(cfg, BY_NAME[field])),
            id=f"{_F}{field}",
            compact=True,
            classes=width,
        )

    def _demo(self, cfg: TerminalConfig) -> bool | None:
        """Whether the master is a demo account, as its newest report says; None if unread."""
        app = self.app
        views = app._views() if hasattr(app, "_views") else []
        view = next((v for v in views if v.cfg.terminal_id == cfg.terminal_id), None)
        snap = view.snapshot if view else None
        if snap is None or snap.trade_mode is None:
            return None
        return bool(snap.trade_mode == ACCOUNT_TRADE_MODE_DEMO)

    def compose(self) -> ComposeResult:
        cfg = self._current()
        title = Text(spaced("settings"), style=f"bold {FG}")
        title.append(f"   {self._name}", style="bold")
        with VerticalScroll(id="dialog", classes="settings", can_focus=False):
            yield Horizontal(
                Static(title, id="st-title"),
                Static(Text("master", style=MUTED), id="st-where"),
                id="st-head",
            )
            yield Static(
                Text(
                    "Its trades are opened and closed on every slave; sizing, renames and "
                    "what gets copied are set on each slave. The copier only reads the master "
                    "unless its breakeven is on.",
                    style=MUTED,
                ),
                id="st-mode",
            )
            spec = BY_NAME["management"]
            yield section(
                "open trades",
                rows(
                    lead("Once a trade is open:"),
                    Choice(spec.choices, str(initial_text(cfg, spec)), id=f"{_F}management"),
                ),
                Static("", id="management-says"),
                Static("", id=f"{_E}management", classes="err"),
            )
            yield section(
                "breakeven",
                rows(
                    lead("Move the master's stop past entry:"),
                    Choice(
                        _BREAKEVEN,
                        str(initial_text(cfg, BY_NAME["breakeven_mode"])),
                        id=f"{_F}breakeven_mode",
                    ),
                    part(
                        self._input("breakeven_trigger_r", cfg, "n5"),
                        words("R in profit."),
                        id="m-trigger-part",
                    ),
                ),
                rows(
                    part(
                        lead("Then close"),
                        self._input("breakeven_book_percent", cfg, "n5"),
                        words("% of it to book profit (0: none)."),
                        id="m-book-row",
                    ),
                ),
                Static("", id="m-be-says"),
                *[
                    Static("", id=f"{_E}{f}", classes="err")
                    for f in ("breakeven_mode", "breakeven_trigger_r", "breakeven_book_percent")
                ],
            )
            yield section(
                "symbols",
                rows(
                    lead("This broker's prefix"),
                    self._input("symbol_prefix", cfg),
                    words("and suffix"),
                    self._input("symbol_suffix", cfg),
                ),
                *[Static("", id=f"{_E}{f}", classes="err") for f in _SYMBOL_FIELDS],
            )
            yield Horizontal(
                Static("EXAMPLE", id="ex-name"),
                Static("", id="ex-body"),
                id="example",
            )
            yield Static("", id="help")
            yield Static("", id="general-error")
            with Horizontal(id="st-foot"):
                yield Static(
                    Text("Nothing open is given up: copies stay managed.", style=MUTED),
                    id="st-stake",
                )
                yield Button("Save  ctrl+s", id="save", classes="keep")
                yield Button("Cancel  esc", id="cancel", classes="plain")

    def on_mount(self) -> None:
        self._show_example()
        self._show_management()
        self._show_breakeven()
        self.query_one(f"#{_F}management").focus()

    def _show_management(self) -> None:
        master = self.query_one(f"#{_F}management", Choice).value == "master"
        self.query_one("#management-says", Static).update(
            Text(
                "Slaves follow the master's stop and target moves and partial closes, and their "
                "own breakeven is off. A slave can still keep its own trades, on its settings."
                if master
                else "Slaves copy only opens and closes, then manage the trade themselves with "
                "their own breakeven and booking; the master's stop moves and partial closes are "
                "not copied.",
                style=MUTED,
            ).append(" Applies to trades opened from now on.", style=MUTED)
        )

    def _show_breakeven(self) -> None:
        """The trigger and booking only with breakeven on, and what turning it on means."""
        on = self.query_one(f"#{_F}breakeven_mode", Choice).value == BreakevenMode.R.value
        self.query_one("#m-trigger-part").display = on
        self.query_one("#m-book-row").display = on
        says = Text()
        if on:
            says.append(
                "The copier will trade the master: once a trade is that far in profit it moves "
                "its stop past entry by the trade's commission (doubled for the way out), once "
                "and never looser, and closes the share above. After that the stop is yours. "
                "Slaves the master manages follow both. Only while copying is live.",
                style=MUTED,
            )
            cfg = self._current()
            if self._demo(cfg) is not True and not cfg.allow_live_trading:
                says.append(
                    " This master is not known to be a demo account, so its worker refuses to "
                    "trade it until live trading is allowed on it.",
                    style=f"bold {RED}",
                )
        else:
            says.append("Off: the master is only read from.", style=MUTED)
        self.query_one("#m-be-says", Static).update(says)

    @on(Choice.Changed)
    def _on_choice(self) -> None:
        if self.is_mounted:
            self._show_management()
            self._show_breakeven()

    def _show_example(self) -> None:
        self.query_one("#ex-body", Static).update(
            example(
                self._symbols,
                *(self.query_one(f"#{_F}{f}", Input).value for f in _SYMBOL_FIELDS),
            )
        )

    @on(Input.Changed)
    def _on_changed(self) -> None:
        if self.is_mounted:
            self._show_example()

    # --- help follows focus -----------------------------------------------------------------

    @on(events.DescendantFocus)
    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        name = (event.widget.id or "").removeprefix(_F)
        if name in CHOICES:
            spec = BY_NAME[name]
            line = Text(spec.label, style=f"bold {FG}")
            line.append(f" · {spec.help}", style=MUTED)
            self.query_one("#help", Static).update(line)
            return
        if name not in _SYMBOL_FIELDS:
            if name in BY_NAME:
                spec = BY_NAME[name]
                line = Text(spec.label, style=f"bold {FG}")
                line.append(f" · {spec.help}", style=MUTED)
                self.query_one("#help", Static).update(line)
            return
        line = Text()
        line.append("Taken off the master's names", style=f"bold {FG}")
        line.append(
            " · only where a name has it, so undecorated symbols such as indices stay as they "
            "are. Blank if this broker adds nothing. Applies from the next tick.",
            style=MUTED,
        )
        self.query_one("#help", Static).update(line)

    # --- actions ----------------------------------------------------------------------------

    def _collect(self) -> dict[str, str | bool]:
        raw: dict[str, str | bool] = {f: self.query_one(f"#{_F}{f}", Input).value for f in FIELDS}
        for name in CHOICES:
            raw[name] = self.query_one(f"#{_F}{name}", Choice).value
        return raw

    def _show_general(self, message: str) -> None:
        general = self.query_one("#general-error", Static)
        general.update(Text(message, style=f"bold {RED}"))
        general.display = bool(message)

    def action_save(self) -> None:
        outcome = apply_edits(self._current(), self._collect())
        for f in (*FIELDS, *CHOICES):
            message = outcome.errors.get(f, "")
            line = self.query_one(f"#{_E}{f}", Static)
            line.update(Text(message, style=RED) if message else "")
            line.display = bool(message)
        self._show_general(outcome.errors.get("", ""))
        if not outcome.ok:
            return
        self._apply(outcome, self._applied)

    def _applied(self, refusal: str | None) -> None:
        if refusal is not None:
            self._show_general(refusal)
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_panic(self) -> None:
        self._halt()
        self.dismiss(False)

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


def example(symbols: list[str], prefix: str, suffix: str) -> Text:
    """What the slaves will be given for a few of the master's names, with these settings."""
    out = Text()
    prefix, suffix = prefix.strip(), suffix.strip()
    if not symbols:
        out.append("The master has nothing open to show this on yet. ", style=MUTED)
        symbols = [f"{prefix}EURUSD{suffix}"] if prefix or suffix else ["EURUSD"]
    for i, symbol in enumerate(symbols[:_SHOWN]):
        if i:
            out.append(" · ", style=MUTED)
        plain = undecorate(symbol, prefix, suffix)
        out.append(symbol, style=FG)
        if plain != symbol:
            out.append(" → ", style=MUTED)
            out.append(plain, style=f"bold {FG}")
        else:
            out.append(" as it is", style=MUTED)
    return out
