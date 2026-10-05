"""The guards page: ``k`` on a slave. Its account's prop-firm rules, how each stands, and the
keys that add, change and remove them.

What a rule means and how it is measured is :mod:`mt5copier.guards`'s; this draws the report
the engine gives and edits ``guards.json`` through the app. The form that edits a rule is built
from the parameters the rule declares, so a new rule needs nothing here.

Display only, in this stage: the page says so, and nothing on it can stop or close a trade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from ..config import Role, account_key
from ..guards import REGISTRY, AccountGuards, GuardReport, GuardRule, Level, RuleStatus
from ..guards.base import money
from ..guards.params import Param, ParamKind, ParamValue, resolve
from ..terminal_detail import bar
from .settings import Choice, Tick, lead, rows, section, words
from .style import BLUE, FG, MUTED, RED, YELLOW, key_hint, left_right, spaced
from .widgets import ButtonRow, KeyBar, QuickInput, TopBar

if TYPE_CHECKING:
    from .app import CopierApp

LEVEL_COLOR = {
    Level.OFF: MUTED,
    Level.UNKNOWN: MUTED,
    Level.OK: BLUE,
    Level.NEAR: YELLOW,
    Level.BREACHED: RED,
}

_F = "f-"
_E = "e-"

_COLUMNS = ("RULE · WHEN REACHED", "STANDING", "NOW")
_WIDE = 120
"""The page width from which the table has room for what each rule found, NOW."""

ACTS = (
    "The guards act on new trades: past the point a rule acts at, the slave takes no new "
    "trades, or a new trade is declined or shrunk. What it already holds is still managed. "
)
CLOSING_LATER = (
    "Closing copies comes later: until then, a rule set to close the worst copy or every copy "
    "keeps the slave from new trades instead. "
)
SAFETY_NET = (
    "A firm checks every tick on its own server; the copier sees a few snapshots a second, and "
    "nothing while it is closed, so a gap can jump any limit. Treat these as a safety net."
)


# --- what a row says --------------------------------------------------------------------------


def standing(status: RuleStatus, width: int = 12) -> Text:
    """``█████░░░░░░░ 41%``, or the word for a rule that is not a fraction of a limit."""
    color = LEVEL_COLOR[status.level]
    if status.level is Level.OFF:
        return Text("off", style=MUTED)
    if status.level is Level.UNKNOWN:
        return Text("unknown", style=MUTED)
    fraction = status.fraction
    if fraction is None:
        word = {Level.OK: "ok", Level.NEAR: "check", Level.BREACHED: "breached"}[status.level]
        return Text(word, style=f"bold {color}")
    out = Text(bar(fraction, width), style=color)
    out.append(f" {fraction:.0%}", style=f"bold {color}" if status.level is not Level.OK else MUTED)
    return out


def used_of(status: RuleStatus, currency: str) -> Text:
    """``412.00 of 500.00 USD``, or just the limit where nothing is used yet."""
    if status.limit is None:
        return Text("")
    if status.used is None:
        return Text(f"up to {money(status.limit, currency)}", style=MUTED)
    return Text(f"{money(status.used)} of {money(status.limit, currency)}", style=MUTED)


def two_lines(detail: str, width: int = 46) -> str:
    """A rule's ``a · b · c`` detail, broken at its dots onto a second line when it is long,
    so the table stays inside the page."""
    parts = detail.split(" · ")
    first = parts[0]
    rest = parts[1:]
    while rest and len(first) + 3 + len(rest[0]) <= width:
        first += " · " + rest.pop(0)
    return first + ("\n" + " · ".join(rest) if rest else "")


def rule_cells(status: RuleStatus, currency: str) -> list[Text]:
    """A rule's row: what it is and what it will do, how it stands, and what it found."""
    title = Text(status.title, style=f"bold {FG if status.enabled else MUTED}")
    if status.on_breach:
        title.append(f"\n{status.on_breach}", style=MUTED)
        if status.act_at is not None:
            title.append(f" at {status.act_at:.0%}", style=MUTED)
    stands = standing(status)
    if status.holds_back:
        stands.append("  NO NEW TRADES", style=f"bold {RED}")
    stands.append("\n")
    stands.append_text(used_of(status, currency))
    color = YELLOW if status.level is Level.UNKNOWN and status.enabled else MUTED
    now = Text()
    if status.latched is not None and status.enabled:
        held = "held until the reset" if status.latched == "reset" else "held until cleared"
        now.append(f"{held} · r clears it\n", style=f"bold {RED}")
        now.append(status.detail.split(" · ")[0], style=color)
    else:
        now.append(two_lines(status.detail), style=color)
    return [title, stands, now]


def rule_summary(report: GuardReport) -> Text:
    """The heading's right side: how many rules, and whether the set is on."""
    n = len(report.rules)
    out = Text(f"{n} rule{'' if n == 1 else 's'}", style=MUTED)
    if not report.enabled:
        out.append(" · ", style=MUTED)
        out.append("ALL OFF", style=f"bold {YELLOW}")
    return out


# --- the page ---------------------------------------------------------------------------------


class GuardsScreen(Screen[None]):
    """One slave's account's guards. Holds only the terminal id; the report is read each tick."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("backspace", "app.pop_screen", "Back", show=False),
        Binding("a", "add", "Add a rule"),
        Binding("enter", "edit", "Change"),
        Binding("x", "remove", "Remove"),
        Binding("space", "toggle_rule", "Rule on/off"),
        Binding("o", "toggle_all", "All on/off"),
        Binding("i", "initial_balance", "Initial balance"),
        Binding("t", "templates", "Templates"),
        Binding("c", "copy_from", "Copy from"),
        Binding("r", "release", "Clear a held breach"),
    ]

    def __init__(self, terminal_id: str) -> None:
        super().__init__()
        self.terminal_id = terminal_id
        self._ids: list[str] = []
        """The rule ids, in the order the table lists them."""
        self._wide: bool | None = None
        """Whether the table was laid out with its NOW column."""
        self._shown: GuardReport | None = None
        """The report the table was last drawn from."""
        self._asking: str | None = None
        """What the line under the table is asking for: "initial" or "template"."""

    @property
    def copier(self) -> CopierApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield TopBar("", id="topbar")
        with VerticalScroll(id="gd-page"):
            yield Static("", id="gd-band")
            yield Static(Text(ACTS + CLOSING_LATER + SAFETY_NET, style=MUTED), id="gd-note")
            yield Static("", id="gd-account")
            yield Static("", id="gd-rules-head", classes="heading")
            yield DataTable(id="gd-rules", cursor_type="row", zebra_stripes=False)
            yield Static("", id="gd-said")
            yield Static("", id="gd-empty")
            with Horizontal(id="gd-ask"):
                yield Static("", id="gd-ask-what")
                yield QuickInput(id="gd-ask-box", compact=True)
                yield Static(Text("  enter keep it · esc leave it", style=MUTED), classes="w")
            yield Static("", id="gd-ask-error")
        yield KeyBar("", id="keybar")

    def on_mount(self) -> None:
        table = self.query_one("#gd-rules", DataTable)
        self.set_interval(0.5, self._on_timer)
        self.refresh_page()
        table.focus()

    def _on_timer(self) -> None:
        try:
            self.refresh_page()
        except NoMatches:  # the screen is being torn down under the tick
            pass

    # --- reading -------------------------------------------------------------------------

    def _report(self) -> GuardReport:
        return self.copier._engine.guard_report(self.terminal_id)

    def _key(self) -> str | None:
        return self._report().account_key

    def _guards(self) -> AccountGuards:
        return self.copier._guards.for_account(self._key())

    def _selected_rule(self) -> GuardRule | None:
        table = self.query_one("#gd-rules", DataTable)
        if not self._ids or not table.row_count:
            return None
        rule_id = self._ids[min(table.cursor_row, len(self._ids) - 1)]
        return next((r for r in self._guards().rules if r.id == rule_id), None)

    def refresh_page(self) -> None:
        app = self.copier
        name = app._cfg.display_name(self.terminal_id)
        self.query_one("#topbar", TopBar).update(app.topbar(crumb=f"{name} · guards"))
        report = self._report()

        band = Text(spaced("guards"), style="bold")
        band.append(f"   {name}", style="bold")
        where = Text(f"account {report.account_key}" if report.account_key else "account not known")
        self.query_one("#gd-band", Static).update(left_right(band, where))

        account = Text("Initial balance  ", style=MUTED)
        if report.initial_balance is None:
            account.append("not set", style=f"bold {YELLOW}")
            account.append(" · percent limits are usually taken of it", style=MUTED)
        else:
            account.append(money(report.initial_balance, report.currency), style="bold")
        account.append("   ")
        account.append("i", style=f"bold underline {FG}")
        account.append(" change", style=MUTED)
        self.query_one("#gd-account", Static).update(account)

        self.query_one("#gd-rules-head", Static).update(
            left_right(Text("RULES", style="bold"), rule_summary(report))
        )
        table = self.query_one("#gd-rules", DataTable)
        # A narrow page leaves NOW out of the table; the line under it says it instead.
        wide = self.size.width >= _WIDE
        if wide != self._wide:
            self._wide = wide
            table.clear(columns=True)
            for label in _COLUMNS[: 3 if wide else 2]:
                table.add_column(Text(label, style=f"bold {FG}"), key=label)
        cursor = table.cursor_row
        table.clear()
        self._ids = [s.rule_id for s in report.rules]
        for status in report.rules:
            cells = rule_cells(status, report.currency)
            # Two lines of a rule and a blank one, so where one rule ends is plain to see.
            table.add_row(*cells[: 3 if wide else 2], key=status.rule_id, height=3)
        if self._ids:
            table.move_cursor(row=min(cursor, len(self._ids) - 1), animate=False)
        table.display = bool(self._ids)
        self._shown = report
        self._show_said()
        empty = self.query_one("#gd-empty", Static)
        empty.display = not self._ids
        if report.account_key is None:
            empty.update(
                Text(
                    "The account logged into this terminal is not known yet. Connect it once, "
                    "and its guards can be set here.",
                    style=YELLOW,
                )
            )
        else:
            said = Text("No rules yet. ", style=MUTED)
            for key, words in (
                ("a", "add one"),
                ("t", "apply a template"),
                ("c", "copy a slave's"),
            ):
                said.append(key, style=f"bold underline {FG}")
                said.append(f" {words}  ", style=MUTED)
            empty.update(said)
        self._refresh_keybar(report)

    @on(DataTable.RowHighlighted, "#gd-rules")
    def _show_said(self) -> None:
        """The selected rule as a sentence, under the table: too long for a column."""
        said = self.query_one("#gd-said", Static)
        rules = self._shown.rules if self._shown is not None else ()
        table = self.query_one("#gd-rules", DataTable)
        chosen = rules[min(table.cursor_row, len(rules) - 1)] if rules else None
        said.display = chosen is not None
        if chosen is not None:
            line = Text(f"{chosen.title}: ", style=f"bold {FG}")
            line.append(chosen.sentence, style=MUTED)
            if not self._wide and chosen.detail:
                line.append(f"\nNow: {chosen.detail}", style=MUTED)
            said.update(line)

    def _refresh_keybar(self, report: GuardReport) -> None:
        keys = [key_hint("a", "add a rule", primary=True), key_hint("esc", "back")]
        if report.rules:
            keys += [
                key_hint("enter", "change"),
                key_hint("space", "rule on/off"),
                key_hint("x", "remove"),
                key_hint("o", "all on" if not report.enabled else "all off"),
            ]
        keys += [key_hint("i", "initial balance"), key_hint("t", "templates")]
        if any(r.latched for r in report.rules):
            keys.append(key_hint("r", "clear a held breach"))
        keys += [key_hint("c", "copy from"), key_hint("?", "all keys")]
        self.query_one("#keybar", KeyBar).update(self.copier.keybar(keys))

    # --- writing -------------------------------------------------------------------------

    def _store(self, guards: AccountGuards, said: str) -> bool:
        key = self._key()
        if key is None:
            self.notify(
                "this terminal's account is not known yet: connect it once", severity="warning"
            )
            return False
        if not self.copier.save_guards(key, guards):
            return False
        self.notify(said)
        self.refresh_page()
        return True

    def _need_account(self) -> bool:
        if self._key() is None:
            self.notify(
                "this terminal's account is not known yet: connect it once", severity="warning"
            )
            return False
        return True

    def action_add(self) -> None:
        if not self._need_account():
            return
        options = [
            (t, f"{r.title} · {r.sentence(resolve(r.params, {})[0], '')}")
            for t, r in REGISTRY.items()
        ]

        def picked(type_name: str | None) -> None:
            if type_name is not None:
                self._open_form(GuardRule(type=type_name), new=True)

        self.app.push_screen(GuardPick("ADD A RULE", options), picked)

    @on(DataTable.RowSelected, "#gd-rules")
    def _on_row_chosen(self, event: DataTable.RowSelected) -> None:
        """Enter on the table: the table takes the key itself, and says so with this."""
        event.stop()
        self.action_edit()

    def action_edit(self) -> None:
        rule = self._selected_rule()
        if rule is not None:
            self._open_form(rule, new=False)

    def _open_form(self, rule: GuardRule, *, new: bool) -> None:
        if rule.type not in REGISTRY:
            self.notify(f"{rule.type!r} is not a rule this version knows; it is kept as it is")
            return

        def saved(result: GuardRule | None) -> None:
            if result is None:
                return
            guards = self._guards()
            if new:
                rules = (*guards.rules, result)
            else:
                rules = tuple(result if r.id == result.id else r for r in guards.rules)
            title = REGISTRY[result.type].title
            self._store(guards.model_copy(update={"rules": rules}), f"{title}: saved")

        name = self.copier._cfg.display_name(self.terminal_id)
        self.app.push_screen(RuleForm(rule, name, self._report().currency), saved)

    def action_remove(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        title = REGISTRY[rule.type].title if rule.type in REGISTRY else rule.type

        def answered(yes: bool | None) -> None:
            if yes:
                guards = self._guards()
                rules = tuple(r for r in guards.rules if r.id != rule.id)
                self._store(guards.model_copy(update={"rules": rules}), f"{title}: removed")

        self.app.push_screen(
            GuardConfirm(
                f"Remove {title}?", "The rule and how it is set are forgotten.", "Remove it"
            ),
            answered,
        )

    def action_toggle_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        guards = self._guards()
        flipped = rule.model_copy(update={"enabled": not rule.enabled})
        rules = tuple(flipped if r.id == rule.id else r for r in guards.rules)
        title = REGISTRY[rule.type].title if rule.type in REGISTRY else rule.type
        self._store(
            guards.model_copy(update={"rules": rules}),
            f"{title}: {'on' if flipped.enabled else 'off'}",
        )

    def action_release(self) -> None:
        """Let go of the selected rule's held breach, after asking. A daily one lets go at the
        reset by itself; one held until cleared stays until this."""
        rule = self._selected_rule()
        if rule is None or self._key() is None:
            return
        status = next((s for s in self._report().rules if s.rule_id == rule.id), None)
        if status is None or status.latched is None:
            self.notify("that rule is not holding a breach")
            return
        title = status.title

        def answered(yes: bool | None) -> None:
            if yes and self.copier._engine.clear_guard_latch(self.terminal_id, rule.id):
                self.notify(f"{title}: breach cleared")
                self.refresh_page()

        self.app.push_screen(
            GuardConfirm(
                f"Clear {title}'s breach?",
                "The slave may take new trades again as soon as the rule is back under the "
                "point it acts at. Clear it only if the firm has reset this account, or the "
                "breach was a mistake.",
                "Clear it",
            ),
            answered,
        )

    def action_toggle_all(self) -> None:
        guards = self._guards()
        if not guards.rules:
            return
        on_now = not guards.enabled
        self._store(
            guards.model_copy(update={"enabled": on_now}), f"guards {'on' if on_now else 'all off'}"
        )

    # --- the line under the table ------------------------------------------------------------

    def action_initial_balance(self) -> None:
        if not self._need_account():
            return
        value = self._guards().initial_balance
        self._ask("initial", "Initial balance, the account size: ", f"{value:g}" if value else "")

    def _ask(self, what: str, label: str, value: str) -> None:
        self._asking = what
        self.query_one("#gd-ask-what", Static).update(Text(label, style=MUTED))
        box = self.query_one("#gd-ask-box", QuickInput)
        box.restrict = r"[0-9.,]*" if what == "initial" else None
        box.value = value
        self._ask_error("")
        line = self.query_one("#gd-ask")
        line.display = True
        box.focus()
        box.cursor_position = len(value)
        self.call_after_refresh(line.scroll_visible, animate=False)

    @on(QuickInput.Escaped, "#gd-ask-box")
    def _on_ask_left(self, event: QuickInput.Escaped) -> None:
        event.stop()
        self._close_ask()

    @on(Input.Submitted, "#gd-ask-box")
    def _on_ask_given(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if self._asking == "initial":
            if not text:
                done = self._store(
                    self._guards().model_copy(update={"initial_balance": None}),
                    "initial balance cleared",
                )
            else:
                try:
                    value = float(text.replace(",", ""))
                except ValueError:
                    self._ask_error("not an amount")
                    return
                if value <= 0:
                    self._ask_error("must be more than 0")
                    return
                done = self._store(
                    self._guards().model_copy(update={"initial_balance": value}),
                    f"initial balance {money(value)}",
                )
        elif self._asking == "template":
            if not text:
                self._ask_error("give the template a name")
                return
            done = self.copier.save_template(text, self._guards().rules)
            if done:
                self.notify(f"template {text!r} saved")
        else:
            done = True
        if done:
            self._close_ask()

    def _ask_error(self, message: str) -> None:
        line = self.query_one("#gd-ask-error", Static)
        line.update(Text(message, style=RED))
        line.display = bool(message)

    def _close_ask(self) -> None:
        self._asking = None
        self.query_one("#gd-ask").display = False
        self._ask_error("")
        self.query_one("#gd-rules", DataTable).focus()

    # --- templates and copying ---------------------------------------------------------------

    def action_templates(self) -> None:
        if not self._need_account():
            return
        templates = self.copier._guards.templates
        options: list[tuple[str, str]] = []
        if self._guards().rules:
            options.append(("\0save", "save these rules as a template"))
        for name, rules in sorted(templates.items()):
            options.append(
                (name, f"apply {name} · {len(rules)} rule{'' if len(rules) == 1 else 's'}")
            )
        if not options:
            self.notify("no templates yet: add rules, then save them as one with t")
            return

        def picked(choice: str | None) -> None:
            if choice is None:
                return
            if choice == "\0save":
                self._ask("template", "Template name: ", "")
                return
            self._replace_rules(templates[choice], f"template {choice}")

        self.app.push_screen(GuardPick("TEMPLATES", options), picked)

    def action_copy_from(self) -> None:
        if not self._need_account():
            return
        app = self.copier
        here = self._key()
        options: list[tuple[str, str]] = []
        for tcfg in app._cfg.terminals.values():
            key = account_key(tcfg.last_login, tcfg.last_server)
            if tcfg.terminal_id == self.terminal_id or key is None or key == here:
                continue
            if tcfg.role is not Role.SLAVE:
                continue
            rules = app._guards.for_account(key).rules
            if rules:
                options.append(
                    (key, f"{app._cfg.display_name(tcfg.terminal_id)} · {len(rules)} rules")
                )
        if not options:
            self.notify("no other slave has guards to copy yet")
            return

        def picked(key: str | None) -> None:
            if key is not None:
                name = next(words for k, words in options if k == key).split(" · ")[0]
                self._replace_rules(app._guards.for_account(key).rules, name)

        self.app.push_screen(GuardPick("COPY RULES FROM", options), picked)

    def _replace_rules(self, rules: tuple[GuardRule, ...], source: str) -> None:
        """Take ``rules`` in place of this account's own, each under a new id so nothing one
        has recorded is mistaken for the other's. The initial balance stays this account's."""
        fresh = tuple(
            GuardRule(type=r.type, enabled=r.enabled, params=dict(r.params)) for r in rules
        )
        guards = self._guards()

        def answered(yes: bool | None) -> None:
            if yes:
                self._store(guards.model_copy(update={"rules": fresh}), f"rules from {source}")

        if not guards.rules:
            answered(True)
            return
        n = len(guards.rules)
        self.app.push_screen(
            GuardConfirm(
                f"Replace {n} rule{'' if n == 1 else 's'}?",
                f"This account's rules are replaced by the {len(fresh)} from {source}. "
                "Its initial balance is kept.",
                "Replace them",
            ),
            answered,
        )


# --- a list to pick from ----------------------------------------------------------------------


class GuardPick(ModalScreen[str | None]):
    """A short list in a box; enter picks, esc leaves. Returns the picked option's value."""

    DEFAULT_CSS = """
    GuardPick { background: #0c0b09 80%; align: center middle; }
    GuardPick #pick { width: 90; max-width: 100%; height: auto; max-height: 100%;
        border: solid $foreground; background: $background; padding: 1 2; }
    GuardPick #pick-title { margin-bottom: 1; }
    GuardPick OptionList { background: $background; border: none; height: auto;
        max-height: 20; padding: 0; }
    GuardPick OptionList > .option-list--option-highlighted,
    GuardPick OptionList:focus > .option-list--option-highlighted {
        background: $foreground; color: $background; text-style: bold; }
    """

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self._heading = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="pick"):
            yield Static(Text(self._heading, style="bold"), id="pick-title")
            yield OptionList(
                *(Option(words, id=str(n)) for n, (_, words) in enumerate(self._options))
            )

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def action_close(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected)
    def _on_picked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self._options[int(event.option.id or 0)][0])


class GuardConfirm(ModalScreen[bool]):
    """A yes-or-no before something is forgotten. Esc, and the button focus rests on, say no."""

    DEFAULT_CLASSES = "dialog"

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, said: str, go: str) -> None:
        super().__init__()
        self._heading, self._said, self._go = title, said, go

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog", classes="dialog", can_focus=False):
            yield Static(Text(self._heading.upper(), style="bold"), classes="dialog-title")
            yield Static(self._said)
            with ButtonRow(id="buttons"):
                yield Button("Keep it  esc", id="cancel", classes="keep")
                yield Button(self._go, id="go", classes="danger")

    def on_mount(self) -> None:
        self.query_one("#cancel").focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#go")
    def _on_go(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(False)


# --- a rule's form ----------------------------------------------------------------------------


def _field(param: Param, value: object) -> Choice | Input:
    if param.kind is ParamKind.CHOICE:
        return Choice(tuple((w, v) for v, w in param.choices), str(value), id=f"{_F}{param.name}")
    width = {ParamKind.NUMBER: "n10", ParamKind.TIME: "n6", ParamKind.ZONE: "grow"}[param.kind]
    shown = f"{value:g}" if isinstance(value, float) else str(value)
    return Input(value=shown, id=f"{_F}{param.name}", compact=True, classes=width)


class RuleForm(ModalScreen[GuardRule | None]):
    """One rule's parameters, each a line, and the sentence the rule reads as. Dismisses with
    the rule as edited, or None."""

    DEFAULT_CLASSES = "form-screen"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("f9", "panic", "PANIC halt"),
    ]

    def __init__(self, rule: GuardRule, terminal_name: str, currency: str = "") -> None:
        super().__init__()
        self._rule = rule
        self._kind = REGISTRY[rule.type]
        self._terminal_name = terminal_name
        self._currency = currency

    @property
    def copier(self) -> CopierApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        values, _ = self._rule.resolved()
        title = Text(spaced("guard"), style=f"bold {FG}")
        title.append(f"   {self._kind.title}", style="bold")
        lines: list[Widget] = []
        for param in self._kind.params:
            lines.append(
                rows(
                    lead(param.label),
                    _field(param, values[param.name]),
                    words(param.after) if param.after else [],
                    classes=f"p-{param.name}",
                )
            )
            lines.append(Static("", id=f"{_E}{param.name}", classes="err"))
        with VerticalScroll(id="dialog", classes="settings", can_focus=False):
            yield Horizontal(
                Static(title, id="st-title"),
                Static(Text(self._terminal_name, style=MUTED), id="st-where"),
                id="st-head",
            )
            yield Static(Text(ACTS + CLOSING_LATER, style=MUTED), id="st-mode")
            yield section("rule", *lines)
            yield section(
                "on",
                rows(
                    Tick(
                        "this rule is switched on",
                        value=self._rule.enabled,
                        id=f"{_F}enabled",
                        compact=True,
                    )
                ),
            )
            yield Horizontal(
                Static("READS AS", id="ex-name"),
                Static("", id="ex-body"),
                id="example",
            )
            yield Static("", id="general-error")
            with Horizontal(id="st-foot"):
                yield Static("", id="st-stake")
                yield Button("Save  ctrl+s", id="save", classes="keep")
                yield Button("Cancel  esc", id="cancel", classes="plain")

    def on_mount(self) -> None:
        self._show_parts()
        self._reword()
        first = self._kind.params[0] if self._kind.params else None
        if first is not None:
            self.query_one(f"#{_F}{first.name}").focus()

    def _collect(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for param in self._kind.params:
            widget = self.query_one(f"#{_F}{param.name}")
            if isinstance(widget, Choice):
                out[param.name] = widget.value
            elif isinstance(widget, Input):
                out[param.name] = widget.value.replace(",", "").strip()
        return out

    def _checked(self) -> tuple[dict[str, ParamValue], dict[str, str]]:
        """Every value as checked, and the problems. A hidden value is checked too: it is kept
        with the rule, so a slip in it would only surface when it is next shown."""
        values: dict[str, ParamValue] = {}
        errors: dict[str, str] = {}
        for param in self._kind.params:
            value, problem = param.check(self._collect()[param.name])
            if problem is not None or value is None:
                errors[param.name] = problem or f"{param.label} is missing"
            else:
                values[param.name] = value
        return values, errors

    def _show_parts(self) -> None:
        given = self._collect()
        for param in self._kind.params:
            if param.only_if is not None:
                other, wanted = param.only_if
                shown = given.get(other) == wanted
                for widget in self.query(f".p-{param.name}"):
                    widget.display = shown

    def _reword(self) -> None:
        values, errors = self._checked()
        body = self.query_one("#ex-body", Static)
        if errors:
            body.update(Text("Correct the values above to read the rule.", style=MUTED))
            return
        body.update(Text(self._kind.sentence(values, self._currency)))

    @on(Choice.Changed)
    def _on_choice(self) -> None:
        if self.is_mounted:
            self._show_parts()
            self._reword()

    @on(Input.Changed)
    def _on_typed(self) -> None:
        if self.is_mounted:
            self._reword()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_panic(self) -> None:
        self.copier.action_panic()
        self.dismiss(None)

    def action_save(self) -> None:
        values, errors = self._checked()
        for param in self._kind.params:
            line = self.query_one(f"#{_E}{param.name}", Static)
            message = errors.get(param.name, "")
            line.update(Text(message, style=RED))
            line.display = bool(message)
        if errors:
            first = next(p for p in self._kind.params if p.name in errors)
            self.query_one(f"#{_F}{first.name}").focus()
            return
        enabled = bool(self.query_one(f"#{_F}enabled", Tick).value)
        self.dismiss(self._rule.model_copy(update={"params": values, "enabled": enabled}))

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
