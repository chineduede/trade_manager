"""The settings form in the new look: each section a sentence with its values written into it,
and a worked example of what the sizing settings would open.

It is the classic :class:`~mt5copier.tui.settings_screen.SettingsScreen` with its drawing
replaced. Saving, validating, the open-copies question and the panic key all stay the classic
screen's: every field keeps its ``f-`` id and every error line its ``e-`` id, so its handlers
find them. Two things are read back differently, because they are drawn differently -- reversing
trades is a choice of direction here rather than a switch, and the symbol renames are one line
rather than a box -- and help follows focus as a line under the example, not a panel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Select, Static

from ..config import BreakevenMode, NoStopPolicy, Role, SizingMode, TerminalConfig
from ..day_book import MasterEvent
from ..protocol import ACCOUNT_TRADE_MODE_DEMO, ActionKind, PositionType
from ..settings_edit import BY_NAME, apply_edits, copies_word, initial_values
from ..settings_example import Example, MasterTrade, PriceValue, pick, work
from ..tui.settings_screen import SettingsScreen
from .style import BLUE, FG, MUTED, RED, spaced

if TYPE_CHECKING:
    from .app import NewCopierApp

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
_BREAKEVEN = (
    ("never", BreakevenMode.OFF.value),
    ("once it is", BreakevenMode.R.value),
)


class Tick(Checkbox):
    """``[x] label``, as the design draws a switch."""

    @property
    def _button(self) -> Content:
        return Content.styled("[x]" if self.value else "[ ]", f"bold {FG}")


def _words(text: str) -> Static:
    """Words between two values, a space either side of them."""
    return Static(f" {text} ", classes="w")


def _lead(text: str) -> Static:
    """The words a line starts with."""
    return Static(f"{text} ", classes="w")


def _rows(*parts: Widget, classes: str = "") -> Horizontal:
    return Horizontal(*parts, classes=f"line {classes}".strip())


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


class SettingsForm(SettingsScreen):
    """One slave's settings, written out as sentences."""

    @property
    def copier(self) -> NewCopierApp:
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
        if mview and mview.snapshot and mview.snapshot.open_positions:
            for p in sorted(mview.snapshot.open_positions, key=lambda p: p.time, reverse=True):
                held.append(
                    MasterTrade(
                        p.ticket, p.symbol, p.type is PositionType.BUY, p.price_open, p.sl, p.tp
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
    def _select(name: str, choices: tuple[tuple[str, str], ...], value: str) -> Select[str]:
        select: Select[str] = Select(
            choices, value=value, allow_blank=False, id=f"{_F}{name}", compact=True
        )
        # As wide as its longest choice and the arrow, so a sentence reads on past it.
        select.styles.width = max(len(label) for label, _ in choices) + 4
        return select

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

    def _section(self, title: str, *body: Widget) -> Horizontal:
        return Horizontal(
            Static(title.upper(), classes="sect-name"),
            Vertical(*body, classes="sect-body"),
            classes="sect",
        )

    def compose(self) -> ComposeResult:
        cfg = self._current()
        self._gather(cfg)
        v = initial_values(cfg)
        mode = Text()
        if self._live:
            mode.append("LIVE", style=f"bold {RED}")
            mode.append(" A saved change applies to the next trade copied.", style=MUTED)
        else:
            mode.append("DRY RUN", style=f"bold {FG}")
            mode.append(" Nothing is sent either way.", style=MUTED)
        renames = ", ".join(f"{k}={t}" for k, t in sorted(cfg.symbol_map.items()))

        with VerticalScroll(id="dialog", classes="settings", can_focus=False):
            yield self._header(cfg)
            yield Static(mode, id="st-mode")

            yield self._section(
                "size",
                _rows(
                    _lead("Risk"),
                    self._input("risk_percent", v["risk_percent"], "n6"),
                    self._input("risk_cash", v["risk_cash"], "n10"),
                    self._select("sizing_mode", _SIZING, str(v["sizing_mode"])),
                    _words("on each trade, over the master's stop distance."),
                ),
                _rows(
                    _lead("Allow"),
                    self._input(
                        "commission_per_lot_round_turn", v["commission_per_lot_round_turn"], "n8"
                    ),
                    _words("commission per lot, in and out, and"),
                    self._input("slippage_buffer_points", v["slippage_buffer_points"], "n5"),
                    _words("points of slippage."),
                ),
                _rows(
                    _lead("Open at most"),
                    self._input("max_lot", v["max_lot"], "n12"),
                    _words("lots. Accept a fill up to"),
                    self._input("max_deviation_points", v["max_deviation_points"], "n5"),
                    _words("points off the quote."),
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
            yield self._section(
                "no stop",
                _rows(
                    _lead("When the master's trade has no stop:"),
                    self._select("no_stop_policy", _NO_STOP, str(v["no_stop_policy"])),
                    Horizontal(
                        self._input("no_stop_lot_multiple", v["no_stop_lot_multiple"], "n6"),
                        _words("× the master's lots, unprotected."),
                        id="multiple-part",
                        classes="part",
                    ),
                ),
                *self._errors("no_stop_policy", "no_stop_lot_multiple"),
            )
            yield self._section(
                "follow",
                _rows(
                    Tick(
                        "stop and target moves",
                        value=bool(v["copy_sltp"]),
                        id=f"{_F}copy_sltp",
                        compact=True,
                    ),
                    Tick(
                        "partial closes",
                        value=bool(v["copy_partials"]),
                        id=f"{_F}copy_partials",
                        compact=True,
                    ),
                    Tick(
                        "pending orders",
                        value=bool(v["copy_pending"]),
                        id=f"{_F}copy_pending",
                        compact=True,
                    ),
                ),
                *self._errors("copy_sltp", "copy_partials", "copy_pending"),
            )
            yield self._section(
                "direction",
                _rows(
                    _lead("Trade"),
                    self._select("invert", _DIRECTION, "opposite" if cfg.invert else "same"),
                ),
                *self._errors("invert"),
            )
            yield self._section(
                "breakeven",
                _rows(
                    _lead("Move the stop past entry:"),
                    self._select("breakeven_mode", _BREAKEVEN, str(v["breakeven_mode"])),
                    Horizontal(
                        self._input("breakeven_trigger_r", v["breakeven_trigger_r"], "n5"),
                        _words("R in profit."),
                        id="trigger-part",
                        classes="part",
                    ),
                ),
                *self._errors("breakeven_mode", "breakeven_trigger_r"),
            )
            yield self._section(
                "symbols",
                _rows(
                    _lead("Prefix"),
                    self._input("symbol_prefix", v["symbol_prefix"], "n6"),
                    _words("suffix"),
                    self._input("symbol_suffix", v["symbol_suffix"], "n6"),
                    _words("renamed"),
                    Input(
                        value=renames,
                        id=f"{_F}symbol_map",
                        compact=True,
                        placeholder=BY_NAME["symbol_map"].placeholder,
                        classes="grow",
                    ),
                ),
                _rows(
                    _lead("Only copy"),
                    self._input("symbol_whitelist", v["symbol_whitelist"], "grow"),
                    _words("never copy"),
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
        out = Text(
            f"{copies_word(len(held))} on this slave {'is' if len(held) == 1 else 'are'} "
            "orphaned if you save a change.",
            style=RED,
        )
        out.append(" A breakeven change keeps them.", style=MUTED)
        return out

    def on_mount(self, event: events.Mount | None = None) -> None:
        # Not the classic form's too: it would put focus on the sizing mode instead.
        if event is not None:
            event.prevent_default()
        self._show_parts()
        self.query_one(
            f"#{_F}risk_percent"
            if self._mode() is SizingMode.PERCENT_OF_BALANCE
            else f"#{_F}risk_cash"
        ).focus()

    # --- reading back -----------------------------------------------------------------------

    def _collect(self) -> dict[str, str | bool]:
        raw = super()._collect()
        raw["invert"] = str(self.query_one(f"#{_F}invert", Select).value) == "opposite"
        return raw

    def _mode(self) -> SizingMode:
        return SizingMode(str(self.query_one(f"#{_F}sizing_mode", Select).value))

    def _show_parts(self) -> None:
        """Only the values the choices beside them use: the risk the sizing mode reads, the
        multiple when no-stop trades are copied, the trigger when breakeven is on."""
        percent = self._mode() is SizingMode.PERCENT_OF_BALANCE
        self.query_one(f"#{_F}risk_percent").display = percent
        self.query_one(f"#{_F}risk_cash").display = not percent
        policy = str(self.query_one(f"#{_F}no_stop_policy", Select).value)
        self.query_one("#multiple-part").display = policy == NoStopPolicy.LOT_MULTIPLE.value
        be = str(self.query_one(f"#{_F}breakeven_mode", Select).value)
        self.query_one("#trigger-part").display = be == BreakevenMode.R.value

    def _rework(self) -> None:
        outcome = apply_edits(self._current(), self._collect())
        self.query_one("#ex-body", Static).update(self._example(outcome.config))

    @on(Select.Changed)
    def _on_choice(self, event: Select.Changed) -> None:
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
            self._rework()

    # --- help follows focus -----------------------------------------------------------------

    @on(events.DescendantFocus)
    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget_id = event.widget.id or ""
        # Not the classic form's too: it would put the bare help back, in a panel this has not.
        event.prevent_default()
        if not widget_id.startswith(_F):
            return
        spec = BY_NAME.get(widget_id.removeprefix(_F))
        if spec is None:
            return
        line = Text()
        line.append(spec.label, style=f"bold {FG}")
        if spec.help:
            line.append(f" · {spec.help}", style=MUTED)
        self.query_one("#help", Static).update(line)
