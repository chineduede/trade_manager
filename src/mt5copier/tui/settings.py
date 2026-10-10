"""A slave's settings form: each section a sentence with its values written into it, and a
worked example of what the sizing settings would open.

A form and nothing else. Every decision about what a value means -- parsing, bounds, what counts
as a change, what deserves a warning -- is in :mod:`mt5copier.settings_edit`, where it can be
tested without a terminal emulator. This module lays the fields out, reads them back, and shows
the messages that module returns. Every field has an ``f-`` id and every error line an ``e-``
id. A choice is every option side by side with the chosen one filled (a dropdown would open
over the sentences below it), reversing trades is a choice of direction rather than a switch,
and the symbol renames are one line rather than a box. Help follows focus, as a line under the
example.

The form is handed callables rather than a config object. ``current`` returns the terminal's
*live* settings and is called at the moment of saving, so an edit is built on what is true then
and not on what was true when the form opened. ``apply`` receives the validated result and a
callback, and reports through the callback how it ended -- None for applied, or a reason it was
refused after all. It reports later, not by returning, because an edit to a terminal that holds
open copies has to ask first. The form stays open until it hears, and if the person declines, it
never does: they are back at the form, with nothing changed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Size
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Static

from ..config import BreakevenMode, Management, NoStopPolicy, Role, SizingMode, TerminalConfig
from ..day_book import MasterEvent
from ..protocol import ACCOUNT_TRADE_MODE_DEMO, ActionKind, PositionType
from ..settings_edit import (
    BY_NAME,
    EDITABLE,
    EditOutcome,
    apply_edits,
    copies_word,
    initial_values,
)
from ..settings_example import Example, MasterTrade, PriceValue, pick, work
from ..symbols import undecorate
from .flow import Flow, Word, flow_words
from .style import BG, BLUE, CHIP_BG, FG, MUTED, RED, spaced

if TYPE_CHECKING:
    from .app import CopierApp

_F = "f-"
_E = "e-"

_SIZING = (
    ("% of balance", SizingMode.PERCENT_OF_BALANCE.value),
    ("in cash", SizingMode.FIXED_CASH_RISK.value),
)
_NO_STOP = (
    ("skip the trade", NoStopPolicy.SKIP.value),
    ("copy it at", NoStopPolicy.LOT_MULTIPLE.value),
)
_DIRECTION = (
    ("the same way as the master", "same"),
    ("the opposite way: buy becomes sell", "opposite"),
)
_LISTS = ("symbol_map", "symbol_whitelist", "symbol_blacklist")
"""One-line lists, where enter starts the next entry instead of saving the form."""

_HELP = {
    "symbol_map": "MASTER=BROKER pairs separated by commas, for symbols the prefix and suffix "
    "cannot express: US30=US30.cash, GER40=DE40. A name the broker does not list is refused, "
    "not guessed at.",
}
"""Help that says more than the field's own words, where a list is typed on one line."""

_BREAKEVEN = (
    ("never", BreakevenMode.OFF.value),
    ("once it is", BreakevenMode.R.value),
)


class Tick(Checkbox):
    """``[x] label``, as the design draws a switch: ticked is bold and cream, unticked is dim, so
    which are on reads at a glance."""

    @property
    def _button(self) -> Content:
        if self.value:
            return Content.styled("[x]", f"bold {FG}")
        return Content.styled("[ ]", MUTED)


class Choice(Widget, can_focus=True):
    """Every option side by side, the chosen one filled. Left and right (or space) choose; so
    does a click on an option. Nothing opens over the text around it, which a dropdown does."""

    BINDINGS = [
        Binding("left", "move(-1)", "Previous", show=False),
        Binding("right", "move(1)", "Next", show=False),
        Binding("space", "move(1)", "Next", show=False),
    ]

    DEFAULT_CSS = """
    Choice {
        width: auto;
        height: auto;
    }
    """

    value: reactive[str] = reactive("")

    class Changed(Message):
        def __init__(self, choice: Choice, value: str) -> None:
            super().__init__()
            self.choice = choice
            self.value = value

        @property
        def control(self) -> Choice:
            return self.choice

    def __init__(self, options: tuple[tuple[str, str], ...], value: str, *, id: str) -> None:
        super().__init__(id=id)
        self._options = options
        self.set_reactive(Choice.value, value)

    def _spans(self) -> list[tuple[str, str]]:
        """Each option's text and style, in order."""
        out = []
        for label, value in self._options:
            if self.disabled:
                style = f"bold {MUTED} on {CHIP_BG}" if value == self.value else MUTED
            elif value == self.value:
                style = f"bold {BG} on {FG}"
            elif self.has_focus:
                style = f"{FG} on {CHIP_BG}"
            else:
                style = f"{MUTED} on {CHIP_BG}"
            out.append((f" {label} ", style))
        return out

    def _side_by_side(self) -> int:
        """The width the options take in one line, with the arrows either side."""
        spans = self._spans()
        return sum(len(text) for text, _ in spans) + len(spans) - 1 + 4

    def _stacked(self) -> int:
        """The width the options take one above another."""
        return max(len(text) for text, _ in self._spans()) + 4

    def _stacks(self, width: int) -> bool:
        return 0 < width < self._side_by_side()

    def render(self) -> Content:
        # Arrows either side while it has focus, saying left and right choose. Their room is
        # kept when it has not, so focusing it does not shift the sentence.
        arrow = (f"bold {FG}", "◂ ", " ▸") if self.has_focus else ("", "  ", "  ")
        if self._stacks(self.size.width):
            # Too narrow for them side by side: one above another, the arrows on the chosen.
            lines = []
            for (text, style), (_, value) in zip(self._spans(), self._options, strict=True):
                mark = value == self.value
                lines.append(
                    Content.assemble(
                        (arrow[1] if mark else "  ", arrow[0]),
                        (text, style),
                        (arrow[2] if mark else "", arrow[0]),
                    )
                )
            return Content("\n").join(lines)
        parts: list[tuple[str, str] | str] = [(arrow[1], arrow[0])]
        for i, span in enumerate(self._spans()):
            if i:
                parts.append(" ")
            parts.append(span)
        parts.append((arrow[2], arrow[0]))
        return Content.assemble(*parts)

    def get_content_width(self, container: Size, viewport: Size) -> int:
        full = self._side_by_side()
        return self._stacked() if self._stacks(container.width) else full

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self._options) if self._stacks(container.width) else 1

    def watch_value(self, value: str) -> None:
        self.post_message(self.Changed(self, value))

    def action_move(self, step: int) -> None:
        values = [v for _, v in self._options]
        self.value = values[(values.index(self.value) + step) % len(values)]

    def on_click(self, event: events.Click) -> None:
        if self._stacks(self.size.width):
            if 0 <= event.y < len(self._options):
                self.value = self._options[event.y][1]
            return
        x = 2
        for (text, _), (_, value) in zip(self._spans(), self._options, strict=True):
            if x <= event.x < x + len(text):
                self.value = value
                return
            x += len(text) + 1

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()


Part = Widget | list[Word]
"""A field, or a run of words."""


def words(text: str) -> list[Word]:
    """Words between two values, a space either side of them."""
    return flow_words(text, before=True)


def lead(text: str) -> list[Word]:
    """The words a line starts with."""
    return flow_words(text)


def _flat(parts: tuple[Part, ...]) -> list[Widget]:
    out: list[Widget] = []
    for part in parts:
        out += part if isinstance(part, list) else [part]
    return out


def rows(*parts: Part, classes: str = "") -> Flow:
    """One sentence of a form, wrapped onto more lines when the terminal is too narrow."""
    return Flow(*_flat(parts), classes=f"line {classes}".strip())


def part(*parts: Part, id: str) -> Flow:
    """A stretch of a sentence that is shown only with one choice, such as the multiple."""
    return Flow(*_flat(parts), id=id, classes="part")


def section(title: str, *body: Widget) -> Horizontal:
    """A ruled-off section: its name on the left, its sentences beside it."""
    return Horizontal(
        Static(title.upper(), classes="sect-name"),
        Vertical(*body, classes="sect-body"),
        classes="sect",
    )


def _trades(master_positions: list[MasterTrade], events_: list[MasterEvent]) -> list[MasterTrade]:
    """The master's trades, newest first, each once: what it holds, then what the day book saw."""
    seen: set[int] = set()
    out: list[MasterTrade] = []
    for t in master_positions:
        if t.ticket not in seen:
            seen.add(t.ticket)
            out.append(t)
    for e in reversed(events_):
        if e.kind not in (ActionKind.OPEN, ActionKind.PLACE) or e.master_ticket in seen:
            continue
        if e.entry <= 0:
            continue
        seen.add(e.master_ticket)
        out.append(
            MasterTrade(e.master_ticket, e.symbol, e.label.startswith("buy"), e.entry, e.sl, e.tp)
        )
    return out


class SettingsForm(ModalScreen[bool]):
    """One slave's settings, written out as sentences. Dismisses with True if something was
    saved."""

    DEFAULT_CLASSES = "form-screen"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        # A modal hides the app's own keys, and the panic key must not be one of them: copying
        # can be live while this form is open.
        Binding("f9", "panic", "PANIC halt"),
    ]

    def __init__(
        self,
        current: Callable[[], TerminalConfig],
        apply: Callable[[EditOutcome, Callable[[str | None], None]], None],
        halt: Callable[[], None],
        live: bool,
    ) -> None:
        super().__init__()
        self._current = current
        self._apply = apply
        self._halt = halt
        self._live = live

    @property
    def copier(self) -> CopierApp:
        return self.app  # type: ignore[return-value]

    # --- what the example is worked on ------------------------------------------------------

    def _gather(self, cfg: TerminalConfig) -> None:
        """The slave's balance, the master's trades and this slave's price values, read once.

        Read when the form opens rather than as it is typed into: the example is about the
        settings, and a trade arriving under it would only move the numbers about.
        """
        app = self.copier
        engine = app._engine
        tid = cfg.terminal_id
        views = {v.cfg.terminal_id: v for v in app._views()}
        view = views.get(tid)
        self._balance = view.snapshot.balance if view and view.snapshot else None

        held: list[MasterTrade] = []
        master = app._cfg.master
        mview = views.get(master.terminal_id) if master else None
        if master and mview and mview.snapshot and mview.snapshot.open_positions:
            for p in sorted(mview.snapshot.open_positions, key=lambda p: p.time, reverse=True):
                # The plain name, as the engine and the day book use it.
                name = undecorate(p.symbol, master.symbol_prefix, master.symbol_suffix)
                held.append(
                    MasterTrade(
                        p.ticket, name, p.type is PositionType.BUY, p.price_open, p.sl, p.tp
                    )
                )
        events_ = engine.day_book()
        trades = _trades(held, events_)

        symbols = {t.ticket: t.symbol for t in trades}
        for e in events_:
            symbols.setdefault(e.master_ticket, e.symbol)
        fills: dict[int, PriceValue] = {}
        for c in engine.open_copies(tid) or ():
            if c.price_value > 0:
                fills[c.master_ticket] = PriceValue(c.price_value, c.point)
        for r in reversed(engine.recent_results()):
            res = r.result
            if res.terminal_id == tid and res.ok and res.price_value:
                fills.setdefault(res.master_ticket, PriceValue(res.price_value, res.point or 0.0))
        self._trades = trades
        self._fills = fills
        self._symbols = symbols

    def _example(self, cfg: TerminalConfig | None) -> Text:
        out = Text()
        if cfg is None:
            out.append("Correct the settings above to see what this slave would open.", MUTED)
            return out
        trade, price = pick(self._trades, self._fills, self._symbols, cfg.invert)
        ex: Example = work(cfg, self._balance, trade, price)
        out.append(ex.lead + "\n")
        out.append(ex.working)
        if ex.result:
            out.append(" ")
            out.append(ex.result, style=f"bold {BLUE}")
        if ex.note:
            out.append(f"\n{ex.note}", style=MUTED)
        return out

    # --- composition ------------------------------------------------------------------------

    def _input(self, name: str, value: str | bool, width: str) -> Input:
        spec = BY_NAME[name]
        return Input(
            value=str(value),
            id=f"{_F}{name}",
            compact=True,
            placeholder=spec.placeholder,
            classes=width,
        )

    @staticmethod
    def _select(name: str, choices: tuple[tuple[str, str], ...], value: str) -> Choice:
        return Choice(choices, value, id=f"{_F}{name}")

    @staticmethod
    def _errors(*names: str) -> ComposeResult:
        for name in names:
            yield Static("", id=f"{_E}{name}", classes="err")

    def _header(self, cfg: TerminalConfig) -> Horizontal:
        app = self.copier
        name = app._cfg.display_name(cfg.terminal_id)
        number = app._terminal_numbers().get(cfg.terminal_id)
        view = next((v for v in app._views() if v.cfg.terminal_id == cfg.terminal_id), None)
        snap = view.snapshot if view else None
        title = Text(spaced("settings"), style=f"bold {FG}")
        title.append(f"   {name}", style="bold")
        where = f"slave {number} of A" if number and cfg.role is Role.SLAVE else cfg.role.value
        if snap is None or snap.trade_mode is None:
            kind = Text("account not read yet", style=MUTED)
        elif snap.trade_mode == ACCOUNT_TRADE_MODE_DEMO:
            kind = Text("demo account", style=MUTED)
        elif cfg.allow_live_trading:
            kind = Text("not a demo: live trading allowed", style=f"bold {RED}")
        else:
            kind = Text("not a demo: held back", style=f"bold {RED}")
        right = Text(f"{where} · ", style=MUTED)
        right.append_text(kind)
        return Horizontal(Static(title, id="st-title"), Static(right, id="st-where"), id="st-head")

    def compose(self) -> ComposeResult:
        cfg = self._current()
        self._gather(cfg)
        v = initial_values(cfg)
        mode = Text()
        if self._live:
            # The copier's mode, not the account's: the header says demo or not, and "LIVE"
            # beside "demo account" read as a contradiction.
            mode.append("LIVE COPYING", style=f"bold {RED}")
            mode.append(
                " · orders are being sent. A saved change applies to the next trade the master "
                "opens.",
                style=MUTED,
            )
        else:
            mode.append("DRY RUN", style=f"bold {FG}")
            mode.append(
                " · copies are only simulated, so no order is sent, before or after a change.",
                style=MUTED,
            )
        renames = ", ".join(f"{k}={t}" for k, t in sorted(cfg.symbol_map.items()))

        with VerticalScroll(id="dialog", classes="settings", can_focus=False):
            yield self._header(cfg)
            yield Static(mode, id="st-mode")

            yield section(
                "size",
                rows(
                    lead("Risk"),
                    self._input("risk_percent", v["risk_percent"], "n6"),
                    self._input("risk_cash", v["risk_cash"], "n10"),
                    self._select("sizing_mode", _SIZING, str(v["sizing_mode"])),
                    words("on each trade, over the master's stop distance."),
                ),
                rows(
                    lead("Allow"),
                    self._input(
                        "commission_per_lot_round_turn", v["commission_per_lot_round_turn"], "n8"
                    ),
                    words("commission per lot, in and out, and"),
                    self._input("slippage_buffer_points", v["slippage_buffer_points"], "n5"),
                    words("points of slippage."),
                ),
                rows(
                    lead("Open at most"),
                    self._input("max_lot", v["max_lot"], "n12"),
                    words("lots. Accept a fill up to"),
                    self._input("max_deviation_points", v["max_deviation_points"], "n5"),
                    words("points off the quote."),
                ),
                *self._errors(
                    "sizing_mode",
                    "risk_percent",
                    "risk_cash",
                    "commission_per_lot_round_turn",
                    "slippage_buffer_points",
                    "max_lot",
                    "max_deviation_points",
                ),
            )
            yield section(
                "no stop",
                rows(
                    lead("When the master's trade has no stop:"),
                    self._select("no_stop_policy", _NO_STOP, str(v["no_stop_policy"])),
                    part(
                        self._input("no_stop_lot_multiple", v["no_stop_lot_multiple"], "n6"),
                        words("× the master's lots, unprotected."),
                        id="multiple-part",
                    ),
                ),
                *self._errors("no_stop_policy", "no_stop_lot_multiple"),
            )
            yield section(
                "follow",
                rows(
                    lead("Once a trade is open,"),
                    Tick(
                        "let the master manage it: its stop and target moves, partial closes",
                        value=bool(v["master_manages"]),
                        id=f"{_F}master_manages",
                        compact=True,
                    ),
                ),
                Static("", id="follow-note", classes="note"),
                rows(
                    lead("Also copy the master's"),
                    Tick(
                        "pending orders",
                        value=bool(v["copy_pending"]),
                        id=f"{_F}copy_pending",
                        compact=True,
                    ),
                ),
                *self._errors("master_manages", "copy_pending"),
            )
            yield section(
                "direction",
                rows(
                    lead("Trade"),
                    self._select("invert", _DIRECTION, "opposite" if cfg.invert else "same"),
                ),
                *self._errors("invert"),
            )
            yield section(
                "breakeven",
                Static("", id="be-note", classes="note"),
                rows(
                    lead("Move the stop past entry:"),
                    self._select("breakeven_mode", _BREAKEVEN, str(v["breakeven_mode"])),
                    part(
                        self._input("breakeven_trigger_r", v["breakeven_trigger_r"], "n5"),
                        words("R in profit."),
                        id="trigger-part",
                    ),
                    classes="be-row",
                ),
                rows(
                    lead("Then close"),
                    self._input("breakeven_book_percent", v["breakeven_book_percent"], "n5"),
                    words("% of the copy to book profit (0: none)."),
                    classes="be-row",
                ),
                rows(
                    lead("Past entry by"),
                    words(
                        "the commission each fill was charged, doubled for the way out, plus "
                        "the slippage points. The commission above is only a backup here, for "
                        "a fill that shows none."
                    ),
                ),
                *self._errors("breakeven_mode", "breakeven_trigger_r", "breakeven_book_percent"),
            )
            yield section(
                "symbols",
                rows(
                    lead("Prefix"),
                    self._input("symbol_prefix", v["symbol_prefix"], "n6"),
                    words("suffix"),
                    self._input("symbol_suffix", v["symbol_suffix"], "n6"),
                    words("renamed"),
                    Input(
                        value=renames,
                        id=f"{_F}symbol_map",
                        compact=True,
                        placeholder=BY_NAME["symbol_map"].placeholder,
                        classes="grow",
                    ),
                ),
                rows(
                    lead("Only copy"),
                    self._input("symbol_whitelist", v["symbol_whitelist"], "grow"),
                    words("never copy"),
                    self._input("symbol_blacklist", v["symbol_blacklist"], "grow"),
                ),
                *self._errors(
                    "symbol_prefix",
                    "symbol_suffix",
                    "symbol_map",
                    "symbol_whitelist",
                    "symbol_blacklist",
                ),
            )
            yield Horizontal(
                Static("EXAMPLE", id="ex-name"),
                Static(self._example(cfg), id="ex-body"),
                id="example",
            )
            yield Static("", id="help")
            yield Static("", id="general-error")
            with Horizontal(id="st-foot"):
                yield Static(self._at_stake(cfg), id="st-stake")
                yield Button("Save  ctrl+s", id="save", classes="keep")
                yield Button("Cancel  esc", id="cancel", classes="plain")

    def _at_stake(self, cfg: TerminalConfig) -> Text:
        held = self.copier._engine.open_copies(cfg.terminal_id)
        if held is None:
            return Text(
                "Could not read what is open on this slave: you are asked before a change "
                "gives anything up.",
                style=RED,
            )
        if not held:
            return Text("Nothing is open on this slave, so a change gives nothing up.", style=MUTED)
        return Text(
            f"{copies_word(len(held))} on this slave "
            f"{'stays' if len(held) == 1 else 'stay'} managed through a change, each as it was "
            "opened.",
            style=MUTED,
        )

    def on_mount(self) -> None:
        self._show_parts()
        self.query_one(
            f"#{_F}risk_percent"
            if self._mode() is SizingMode.PERCENT_OF_BALANCE
            else f"#{_F}risk_cash"
        ).focus()

    # --- reading back -----------------------------------------------------------------------

    def _collect(self) -> dict[str, str | bool]:
        raw: dict[str, str | bool] = {}
        for spec in EDITABLE:
            widget = self.query_one(f"#{_F}{spec.name}")
            if isinstance(widget, Input):
                raw[spec.name] = widget.value
            elif isinstance(widget, Checkbox):
                raw[spec.name] = bool(widget.value)
        for choice in self.query(Choice):
            raw[(choice.id or "").removeprefix(_F)] = choice.value
        raw["invert"] = raw["invert"] == "opposite"
        return raw

    def _show_errors(self, errors: dict[str, str]) -> None:
        for spec in EDITABLE:
            message = errors.get(spec.name, "")
            line = self.query_one(f"#{_E}{spec.name}", Static)
            line.update(f"[red]{escape(message)}[/red]" if message else "")
            line.display = bool(message)
        self._show_general(errors.get(""))

    def _show_general(self, message: str | None) -> None:
        line = self.query_one("#general-error", Static)
        line.update(f"[bold red]{escape(message)}[/]" if message else "")
        line.display = bool(message)

    def _mode(self) -> SizingMode:
        return SizingMode(self.query_one(f"#{_F}sizing_mode", Choice).value)

    def _show_parts(self) -> None:
        """Only the values the choices beside them use: the risk the sizing mode reads, the
        multiple when no-stop trades are copied, the trigger when breakeven is on."""
        percent = self._mode() is SizingMode.PERCENT_OF_BALANCE
        self.query_one(f"#{_F}risk_percent").display = percent
        self.query_one(f"#{_F}risk_cash").display = not percent
        policy = self.query_one(f"#{_F}no_stop_policy", Choice).value
        self.query_one("#multiple-part").display = policy == NoStopPolicy.LOT_MULTIPLE.value
        be = self.query_one(f"#{_F}breakeven_mode", Choice).value
        self.query_one("#trigger-part").display = be == BreakevenMode.R.value
        self._show_management()

    def _show_management(self) -> None:
        """Grey out the slave's own breakeven while the master manages its trades: both gates
        open, the master set to manage and this slave letting it. Said in words either way."""
        master = self.copier._cfg.master
        master_says = master is not None and master.management is Management.MASTER
        letting = bool(self.query_one(f"#{_F}master_manages", Checkbox).value)
        managed = master_says and letting
        for row in self.query(".be-row"):
            row.disabled = managed
        follow_note = self.query_one("#follow-note", Static)
        if not master_says:
            follow_note.update(
                Text(
                    "The master is set to Slaves manage, so this slave manages its own trades "
                    "whatever this says.",
                    style=MUTED,
                )
            )
        else:
            follow_note.update("")
        follow_note.display = not master_says
        be_note = self.query_one("#be-note", Static)
        be_note.update(
            Text(
                "The master manages this slave's trades, so they follow the master's stop and "
                "this breakeven is off. Untick the master managing them, or set the master to "
                "Slaves manage, to use it.",
                style=MUTED,
            )
            if managed
            else ""
        )
        be_note.display = managed

    def _rework(self) -> None:
        outcome = apply_edits(self._current(), self._collect())
        self.query_one("#ex-body", Static).update(self._example(outcome.config))

    @on(Choice.Changed)
    def _on_choice(self) -> None:
        if not self.is_mounted:
            return
        self._show_parts()
        self._rework()

    @on(Input.Changed)
    def _on_typed(self) -> None:
        if self.is_mounted:
            self._rework()

    @on(Checkbox.Changed)
    def _on_ticked(self) -> None:
        if self.is_mounted:
            self._show_management()
            self._rework()

    # --- help follows focus -----------------------------------------------------------------

    @on(events.DescendantFocus)
    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget_id = event.widget.id or ""
        if not widget_id.startswith(_F):
            return
        spec = BY_NAME.get(widget_id.removeprefix(_F))
        if spec is None:
            return
        line = Text()
        line.append(spec.label, style=f"bold {FG}")
        help_text = _HELP.get(spec.name, spec.help)
        if spec.name in _LISTS:
            help_text += " Enter starts the next one; ctrl+s saves."
        if help_text:
            line.append(f" · {help_text}", style=MUTED)
        self.query_one("#help", Static).update(line)

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
        outcome = apply_edits(self._current(), self._collect())
        self._show_errors(outcome.errors)
        if not outcome.ok:
            first = next((s for s in EDITABLE if s.name in outcome.errors), None)
            if first is not None:
                self.query_one(f"#{_F}{first.name}").focus()
            return
        self._apply(outcome, self._applied)

    def _applied(self, refusal: str | None) -> None:
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
        """Enter in a list starts the next entry; anywhere else it saves."""
        event.stop()
        name = (event.input.id or "").removeprefix(_F)
        if name not in _LISTS:
            self.action_save()
            return
        box = event.input
        text = box.value.rstrip()
        if text and not text.endswith(","):
            box.value = text + ", "
        box.cursor_position = len(box.value)
