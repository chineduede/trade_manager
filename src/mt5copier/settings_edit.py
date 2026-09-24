"""The logic behind the per-terminal settings editor, with no UI in it.

The editor is a form; what it does with what was typed is a set of decisions, and decisions
belong somewhere a test can reach without a terminal emulator. This module turns the text in
the form into a validated :class:`~mt5copier.config.TerminalConfig`, or into one message per
field saying why not.

**Validation is pydantic's, not ours.** The bounds on ``risk_percent`` and the rest live on the
model, so the form cannot drift out of step with what ``load()`` will accept -- a value the form
lets through and the schema rejects on the next start would be read as a corrupt config and
wipe the user's settings. All this module adds is the step pydantic cannot do for a text box:
turning ``""`` into "no ceiling" for ``max_lot``, and refusing ``inf`` and ``nan``, which parse
as floats and, for a field with only a lower bound, pass its check.

**Only the fields listed here can be edited.** What is missing is deliberate:

* ``invert`` flips a copy's direction, and a believed copy records the direction it was opened
  in, so flipping it under live copies makes the ledger describe the opposite of reality. It
  needs a guard before it can be edited (ROADMAP.md, "Changing a setting mid-run").
* ``allow_live_trading`` is the demo-only guard's override. A guard that a stray keypress can
  lift is not a guard, so it stays a hand edit with the app closed.
* Role and ``enabled`` already have their own keys, and the symbol settings are a later step.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum

from pydantic import ValidationError

from .config import NoStopPolicy, SizingMode, TerminalConfig


class Kind(StrEnum):
    FLOAT = "float"
    INT = "int"
    OPTIONAL_FLOAT = "optional_float"
    """A number that may be left blank, meaning "not set" -- ``max_lot`` is the only one."""

    CHOICE = "choice"
    FLAG = "flag"


@dataclass(frozen=True)
class EditableField:
    name: str
    """The :class:`TerminalConfig` attribute this edits."""

    label: str
    kind: Kind
    help: str
    choices: tuple[tuple[str, str], ...] = ()
    """``(label, value)`` pairs, for :attr:`Kind.CHOICE` only."""


@dataclass(frozen=True)
class Section:
    title: str
    fields: tuple[EditableField, ...]


SECTIONS: tuple[Section, ...] = (
    Section(
        "Sizing and execution",
        (
            EditableField(
                "sizing_mode",
                "Sizing mode",
                Kind.CHOICE,
                "How a trade with a stop is sized.",
                (
                    ("percent of balance", SizingMode.PERCENT_OF_BALANCE.value),
                    ("fixed cash amount", SizingMode.FIXED_CASH_RISK.value),
                ),
            ),
            EditableField(
                "risk_percent",
                "Risk percent",
                Kind.FLOAT,
                "Percent of this account's balance risked per trade. Percent mode only.",
            ),
            EditableField(
                "risk_cash",
                "Risk cash",
                Kind.FLOAT,
                "Cash risked per trade, in this account's currency. Fixed-cash mode only.",
            ),
            EditableField(
                "max_lot",
                "Max lot",
                Kind.OPTIONAL_FLOAT,
                "Hard ceiling on lots per copy. Blank means no ceiling.",
            ),
            EditableField(
                "commission_per_lot_round_turn",
                "Commission / lot",
                Kind.FLOAT,
                "The full there-and-back cost of one lot, in the account currency.",
            ),
            EditableField(
                "slippage_buffer_points",
                "Slippage buffer",
                Kind.INT,
                "Points of adverse fill to budget for when sizing. 0 assumes none.",
            ),
            EditableField(
                "max_deviation_points",
                "Max deviation",
                Kind.INT,
                "Widest price move to accept when sending, in points. Never changes size.",
            ),
        ),
    ),
    Section(
        "Trades with no stop",
        (
            EditableField(
                "no_stop_policy",
                "Policy",
                Kind.CHOICE,
                "What to do when the master opens a trade with no stop loss.",
                (
                    ("skip the trade", NoStopPolicy.SKIP.value),
                    ("copy at a multiple of the master's lots", NoStopPolicy.LOT_MULTIPLE.value),
                ),
            ),
            EditableField(
                "no_stop_lot_multiple",
                "Lot multiple",
                Kind.FLOAT,
                "Times the master's lots. Used only when the policy copies no-stop trades.",
            ),
        ),
    ),
    Section(
        "Copy behaviour",
        (
            EditableField("copy_sltp", "Copy stop / target moves", Kind.FLAG, ""),
            EditableField("copy_partials", "Copy partial closes", Kind.FLAG, ""),
            EditableField(
                "copy_pending",
                "Copy pending orders",
                Kind.FLAG,
                "Mirror the master's resting orders as well as its open positions.",
            ),
        ),
    ),
)

EDITABLE: tuple[EditableField, ...] = tuple(f for s in SECTIONS for f in s.fields)
_BY_NAME: dict[str, EditableField] = {f.name: f for f in EDITABLE}


def _number(value: float) -> str:
    """Shortest text that reads back as the same float for anything a person would type."""
    return format(value, ".15g")


def initial_text(cfg: TerminalConfig, spec: EditableField) -> str | bool:
    """The form's starting value for one field: text for a text box or choice, bool for a switch."""
    value = getattr(cfg, spec.name)
    if spec.kind is Kind.FLAG:
        return bool(value)
    if spec.kind is Kind.CHOICE:
        return str(value.value)
    if spec.kind is Kind.OPTIONAL_FLOAT:
        return "" if value is None else _number(value)
    if spec.kind is Kind.INT:
        return str(value)
    return _number(value)


def initial_values(cfg: TerminalConfig) -> dict[str, str | bool]:
    return {f.name: initial_text(cfg, f) for f in EDITABLE}


@dataclass(frozen=True)
class Change:
    name: str
    old: object
    new: object


@dataclass(frozen=True)
class EditOutcome:
    config: TerminalConfig | None
    """The validated result, or None if anything was wrong."""

    errors: dict[str, str]
    """One message per offending field. The key ``""`` is for a problem that belongs to no
    single field, which should not occur but must not be lost if it does."""

    changes: tuple[Change, ...]
    """Only fields whose value actually differs. Retyping ``0.50`` over ``0.5`` is not one."""

    @property
    def ok(self) -> bool:
        return self.config is not None


class _EntryError(Exception):
    pass


_BOUNDS = {
    "greater_than": ("greater than", "gt"),
    "greater_than_equal": ("at least", "ge"),
    "less_than": ("less than", "lt"),
    "less_than_equal": ("at most", "le"),
}
"""pydantic's own wording for a bound is "Input should be less than or equal to 100", which is
long enough to lose the ``100`` off the end of a form row -- and the number is the only part
that tells the user what to type instead."""


def _message(err: Mapping[str, object]) -> str:
    bound = _BOUNDS.get(str(err.get("type")))
    ctx = err.get("ctx")
    if bound is not None and isinstance(ctx, Mapping) and bound[1] in ctx:
        limit = ctx[bound[1]]
        shown = _number(limit) if isinstance(limit, int | float) else limit
        return f"must be {bound[0]} {shown}"
    return str(err["msg"])


def _parse(spec: EditableField, raw: str | bool) -> object:
    """Text to a value, or :class:`_EntryError`. Range checks are left to the model."""
    if spec.kind is Kind.FLAG:
        if not isinstance(raw, bool):
            raise _EntryError("expected on or off")
        return raw
    if not isinstance(raw, str):
        raise _EntryError("expected text")
    text = raw.strip()
    if spec.kind is Kind.CHOICE:
        return text
    if text == "":
        if spec.kind is Kind.OPTIONAL_FLOAT:
            return None
        raise _EntryError("required")
    if spec.kind is Kind.INT:
        try:
            return int(text)
        except ValueError:
            raise _EntryError("must be a whole number") from None
    try:
        number = float(text)
    except ValueError:
        hint = " (use a dot for decimals)" if "," in text else ""
        raise _EntryError(f"not a number{hint}") from None
    if not math.isfinite(number):
        raise _EntryError("must be a finite number")
    return number


def apply_edits(current: TerminalConfig, raw: Mapping[str, str | bool]) -> EditOutcome:
    """Apply what the form holds to ``current``, or say what is wrong with it.

    ``raw`` may hold any subset of the editable fields. A field whose text is exactly what the
    form started with is skipped rather than re-parsed, so a value that does not survive a trip
    through text -- one third, say -- is never quietly altered by an edit to some other field.

    ``current`` should be the terminal's *live* settings at the moment of saving, not a copy
    taken when the form opened: everything not edited here is carried over from it, so a role
    change or a rescan that happened while the form was open is not overwritten.

    Naming a field that is not editable raises. That is a bug in the caller, and quietly
    ignoring it would mean a setting the user thinks they changed was not.
    """
    unknown = sorted(set(raw) - set(_BY_NAME))
    if unknown:
        raise ValueError(f"not editable here: {', '.join(unknown)}")

    updates: dict[str, object] = {}
    errors: dict[str, str] = {}
    for name, value in raw.items():
        spec = _BY_NAME[name]
        if value == initial_text(current, spec):
            continue
        try:
            updates[name] = _parse(spec, value)
        except _EntryError as exc:
            errors[name] = str(exc)

    # Validate whatever parsed even if something else did not, so one round trip shows every
    # problem instead of revealing them one at a time. A field that failed to parse is absent
    # from ``updates`` and so keeps its current value here.
    try:
        new = TerminalConfig.model_validate({**current.model_dump(), **updates})
    except ValidationError as exc:
        new = None
        for err in exc.errors():
            key = str(err["loc"][0]) if err["loc"] else ""
            errors.setdefault(key if key in _BY_NAME else "", _message(err))

    if errors or new is None:
        return EditOutcome(config=None, errors=errors, changes=())

    changes = tuple(
        Change(name, getattr(current, name), getattr(new, name))
        for name in updates
        if getattr(current, name) != getattr(new, name)
    )
    return EditOutcome(config=new, errors={}, changes=changes)


def _show(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return _number(value)
    return str(value)


def describe_changes(changes: tuple[Change, ...]) -> str:
    """One line for the log and the notification: ``risk_percent 1 -> 0.5, copy_pending off -> on``."""
    return ", ".join(f"{c.name} {_show(c.old)} -> {_show(c.new)}" for c in changes)


def advisories(outcome: EditOutcome) -> list[str]:
    """Warnings for an edit that is allowed but widens what a slave can lose.

    Neither is a reason to refuse. Both are things the editor makes one keypress away that were
    a deliberate hand edit before, so the moment they happen is the moment to say so.
    """
    if outcome.config is None:
        return []
    cfg = outcome.config
    out: list[str] = []
    changed = {c.name: c for c in outcome.changes}

    if cfg.no_stop_policy is NoStopPolicy.LOT_MULTIPLE and (
        "no_stop_policy" in changed or "no_stop_lot_multiple" in changed
    ):
        out.append(
            f"trades with no stop will now be copied UNPROTECTED at {_number(cfg.no_stop_lot_multiple)}x "
            "the master's lots. The risk setting does not bound them; max lot still does."
        )
    cap = changed.get("max_lot")
    if cap is not None and cap.old is not None and cap.new is None:
        out.append("max lot removed: copy size is now limited only by the risk setting.")
    return out
