"""The logic behind the per-terminal settings editor, with no UI in it.

The editor is a form; what it does with what was typed is a set of decisions, and decisions
belong somewhere a test can reach without a terminal emulator. This module turns the text in
the form into a validated :class:`~mt5copier.config.TerminalConfig`, or into one message per
field saying why not.

**Validation is pydantic's, not ours.** The bounds on ``risk_percent`` and the rest live on the
model, so the form cannot drift out of step with what ``load()`` will accept -- a value the form
lets through and the schema rejects on the next start would be read as a corrupt config and
wipe the user's settings. What this module adds is the step pydantic cannot do for a text box:
turning ``""`` into "no ceiling" for ``max_lot``, refusing ``inf`` and ``nan`` (which parse as
floats and, for a field with only a lower bound, pass its check), and reading a symbol list or
a ``MASTER=BROKER`` map out of free text.

**Only the fields listed here can be edited.** What is missing is deliberate:

* ``allow_live_trading`` is the demo-only guard's override. A guard that a stray keypress can
  lift is not a guard, so it is not a row in this form, where a stray space would reach it. It
  has its own dialog, which asks before anything is changed (see the desktop bridge's
  ``live_override``).
* ``label`` is owned by discovery, which overwrites it on every rescan and at every start. An
  edit would revert silently the next time the app ran.
* ``role`` has its own keys, and ``terminal_id`` and ``exe_path`` are not the user's to set,
  nor are ``ctrader_account_id`` and ``ctrader_live``, which the cTrader login sets.
* ``paused`` has its own key (``p``), not a setting: it is flipped far more often than anything
  here, and gives nothing up (see ``Engine.set_paused``).
* ``enabled`` is no longer offered at all. Pausing replaced it.

**An edit keeps the copies a slave holds, and goes on managing them.** Each copy keeps what it
was opened with -- size, symbol and direction, ``invert`` included, since a copy's reversal is
read back from the copy, and who manages it, the master or its slave -- so most settings reach
only new trades, and the few that act on open copies (copying pending orders, the breakeven and
its buffer) are meant to.
Only disabling a terminal gives copies up (see ``Engine.apply_settings``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum

from pydantic import ValidationError

from .config import (
    AppConfig,
    BreakevenMode,
    Management,
    NoStopPolicy,
    SizingMode,
    TerminalConfig,
    account_key,
)


class Kind(StrEnum):
    FLOAT = "float"
    INT = "int"
    OPTIONAL_FLOAT = "optional_float"
    """A number that may be left blank, meaning "not set" -- ``max_lot`` is the only one."""

    CHOICE = "choice"
    FLAG = "flag"

    TEXT = "text"
    """One line of text with no spaces in it, and blank is a real answer -- a symbol prefix."""

    SYMBOLS = "symbols"
    """Symbol names separated by commas, spaces or new lines."""

    SYMBOL_MAP = "symbol_map"
    """``MASTER=BROKER`` pairs, separated the same way."""


@dataclass(frozen=True)
class EditableField:
    name: str
    """The :class:`TerminalConfig` attribute this edits."""

    label: str
    kind: Kind
    help: str
    choices: tuple[tuple[str, str], ...] = ()
    """``(label, value)`` pairs, for :attr:`Kind.CHOICE` only."""

    placeholder: str = ""
    """What an empty box says it is for."""


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
                placeholder="no ceiling",
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
            EditableField(
                "invert",
                "Reverse trades (invert)",
                Kind.FLAG,
                "Buy becomes sell. Stop and target swap places, so the copy is sized from the "
                "master's TAKE-PROFIT and a trade with no target copies with no stop.",
            ),
            EditableField(
                "master_manages",
                "Let the master manage it",
                Kind.FLAG,
                "Once a trade is open, follow the master's stop and target moves and partial "
                "closes, when the master is set to manage. Off, this slave manages its own trades "
                "after the entry: its breakeven and booking, none of the master's moves. Opens and "
                "closes are copied either way. Applies to trades opened from now on.",
            ),
            EditableField(
                "copy_pending",
                "Copy pending orders",
                Kind.FLAG,
                "Mirror the master's resting orders as well as its open positions.",
            ),
            EditableField(
                "pending_fallback",
                "Rescue refused pendings",
                Kind.FLAG,
                "When this broker refuses a pending copy's price, retry it once as the other "
                "resting type at the same price (a limit as a stop, a stop as a limit), then "
                "once at market. A copy that went to market is an open position: it is closed "
                "if the master cancels the order, and managed normally once the order fills. "
                "Off, a refused pending is simply not copied.",
            ),
            EditableField(
                "tp_net_rr",
                "Keep direct targets' R:R net of costs",
                Kind.FLAG,
                "Direct trading only. Land this slave's target past the ticket's by (rr + 1) "
                "of its own commissions, so a win pays the ticket's ratio after costs -- "
                "worked out once when the trade is first written, and kept through every "
                "later stop move. Off, the ticket's target is sent exactly as written and "
                "commission comes out of the win. A real master's target is never touched "
                "either way.",
            ),
        ),
    ),
    Section(
        "Breakeven",
        (
            EditableField(
                "breakeven_mode",
                "Breakeven",
                Kind.CHOICE,
                "Move a copy's stop past its entry by its commission and slippage buffer once it "
                "is this far in profit, measured on this slave's own fill. After that the stop "
                "only tightens. Only on trades this slave manages: one the master manages "
                "follows the master's stop instead. Changing these keeps open copies and applies "
                "to them too.",
                (
                    ("off", BreakevenMode.OFF.value),
                    ("after a multiple of the risk (R)", BreakevenMode.R.value),
                ),
            ),
            EditableField(
                "breakeven_trigger_r",
                "Trigger, R",
                Kind.FLOAT,
                "Times the copy's opening stop distance: 0.3 is 0.3R. A copy opened with no stop "
                "never triggers. A single position can be given its own price on its detail "
                "page instead.",
            ),
            EditableField(
                "breakeven_book_percent",
                "Book at breakeven, %",
                Kind.FLOAT,
                "Close this percent of the copy when its breakeven fires, to book some profit; "
                "0 books nothing. Once per copy, a hand-set breakeven price included. If the "
                "slice or what it leaves is under the symbol's minimum lot, nothing is closed "
                "and only the stop moves. A refused close is tried again up to 3 times.",
            ),
        ),
    ),
    Section(
        "Symbols",
        (
            EditableField(
                "symbol_prefix",
                "Symbol prefix",
                Kind.TEXT,
                "What this broker puts before every symbol name. Blank if nothing. Applies to "
                "copies opened from now on.",
            ),
            EditableField(
                "symbol_suffix",
                "Symbol suffix",
                Kind.TEXT,
                "What this broker puts after every symbol name, such as p. Blank if nothing. "
                "Applies to copies opened from now on.",
            ),
            EditableField(
                "symbol_map",
                "Symbol map",
                Kind.SYMBOL_MAP,
                "One MASTER=BROKER pair per line, for symbols the prefix and suffix cannot "
                "express. MASTER is the name without the master's own prefix or suffix. A name "
                "the broker does not list is refused, not guessed at.",
                placeholder="US30=US30.cash",
            ),
            EditableField(
                "symbol_whitelist",
                "Only copy these",
                Kind.SYMBOLS,
                "Master symbol names, without the master's own prefix or suffix, separated by "
                "commas or spaces. Blank means all. Adding one "
                "does not copy a trade that was already declined.",
                placeholder="all symbols",
            ),
            EditableField(
                "symbol_blacklist",
                "Never copy these",
                Kind.SYMBOLS,
                "Master symbol names, without the master's own prefix or suffix, separated by "
                "commas or spaces. Removing one does not copy "
                "a trade that was already declined.",
                placeholder="none",
            ),
        ),
    ),
)

EDITABLE: tuple[EditableField, ...] = tuple(f for s in SECTIONS for f in s.fields)
"""A slave's fields, every one of which its form shows."""

MASTER_FIELDS: tuple[EditableField, ...] = (
    EditableField(
        "management",
        "Who manages open trades",
        Kind.CHOICE,
        "Master manages: slaves follow the master's stop and target moves and partial closes, "
        "and their own breakeven is off. Slaves manage: slaves copy only opens and closes, and "
        "manage the rest with their own breakeven. A slave can always keep its own trades. "
        "Applies to trades opened from now on.",
        (
            ("Master manages", Management.MASTER.value),
            ("Slaves manage", Management.SLAVES.value),
        ),
    ),
)
"""The master's own fields, beside its symbol decoration (which a slave has too)."""

BY_NAME: dict[str, EditableField] = {f.name: f for f in (*EDITABLE, *MASTER_FIELDS)}
"""Every editable field by name, for callers that need one field rather than a section."""


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
    if spec.kind is Kind.TEXT:
        return str(value)
    if spec.kind is Kind.SYMBOLS:
        return ", ".join(value)
    if spec.kind is Kind.SYMBOL_MAP:
        return "\n".join(f"{key}={target}" for key, target in sorted(value.items()))
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


def _float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        hint = " (use a dot for decimals)" if "," in text else ""
        raise _EntryError(f"not a number{hint}") from None
    if not math.isfinite(value):
        raise _EntryError("must be a finite number")
    return value


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


_SEPARATORS = re.compile(r"[\s,;]+")
_AROUND_EQUALS = re.compile(r"\s*=\s*")


def _tokens(text: str) -> list[str]:
    return [token for token in _SEPARATORS.split(text.strip()) if token]


def _parse_symbols(text: str) -> list[str]:
    """Names in the order given, each once. Matching is case-insensitive, so ``eurusd`` and
    ``EURUSD`` are one entry, and the first spelling is the one kept."""
    seen: dict[str, str] = {}
    for token in _tokens(text):
        if "=" in token:
            raise _EntryError(f"{token!r} has an '=' in it; a mapping belongs in the symbol map")
        seen.setdefault(token.upper(), token)
    return list(seen.values())


def _parse_symbol_map(text: str) -> dict[str, str]:
    """``MASTER=BROKER`` pairs. Spaces around the ``=`` are forgiven.

    Keys come back upper-cased, which is what the model does to them anyway. Two keys that differ
    only in case and agree on the target are one entry; two that disagree are refused, because
    choosing between them would be a guess about which instrument was meant.
    """
    pairs: dict[str, str] = {}
    for token in _tokens(_AROUND_EQUALS.sub("=", text)):
        key, sep, target = token.partition("=")
        if not sep or not key or not target or "=" in target:
            raise _EntryError(f"{token!r} is not a MASTER=BROKER pair")
        folded = key.upper()
        if folded in pairs and pairs[folded] != target:
            raise _EntryError(f"{key} is mapped to both {pairs[folded]} and {target}")
        pairs[folded] = target
    return pairs


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
    if spec.kind is Kind.TEXT:
        if any(ch.isspace() for ch in text):
            raise _EntryError("no spaces allowed")
        return text
    if spec.kind is Kind.SYMBOLS:
        return _parse_symbols(text)
    if spec.kind is Kind.SYMBOL_MAP:
        return _parse_symbol_map(text)
    if text == "":
        if spec.kind is Kind.OPTIONAL_FLOAT:
            return None
        raise _EntryError("required")
    if spec.kind is Kind.INT:
        try:
            return int(text)
        except ValueError:
            raise _EntryError("must be a whole number") from None
    return _float(text)


def copies_word(count: int) -> str:
    return f"{count} open {'copy' if count == 1 else 'copies'}"


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
    unknown = sorted(set(raw) - set(BY_NAME))
    if unknown:
        raise ValueError(f"not editable here: {', '.join(unknown)}")

    updates: dict[str, object] = {}
    errors: dict[str, str] = {}
    for name, value in raw.items():
        spec = BY_NAME[name]
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
            errors.setdefault(key if key in BY_NAME else "", _message(err))

    if errors or new is None:
        return EditOutcome(config=None, errors=errors, changes=())

    changes = tuple(
        Change(name, getattr(current, name), getattr(new, name))
        for name in updates
        if getattr(current, name) != getattr(new, name)
    )
    return EditOutcome(config=new, errors={}, changes=changes)


ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_ -]{0,31}$")
"""The profile-name rule (see ``profiles._NAME_RE``) plus spaces, which a display name like
"FTMO 100k" needs and a directory name cannot have. The name is cosmetic, so this is not
protecting anything -- it is one rule for every name the app asks for, instead of two."""

ACCOUNT_NAME_RULE = (
    "an account name is letters, digits, spaces, - or _ (up to 32, starting with a letter or digit)"
)


def rename(cfg: AppConfig, terminal_id: str, name: str) -> str | None:
    """Set (or, given blank, clear) the name for whichever *account* this terminal is last known
    to be logged into -- see :func:`~mt5copier.config.account_key`'s own docstring for why a name
    follows the account, not the terminal. Refuses when no account is known yet (discovery has
    never read a login off this terminal) -- there is nothing to attach a name to -- and refuses
    a name outside :data:`ACCOUNT_NAME_RE`.

    Not routed through :func:`apply_edits`: a name is cosmetic, never read by the diff engine or
    the executor, so gating it behind the orphan-confirm step that guards every *trading*
    setting would ask a person to give up open copies to rename a terminal. Same reasoning
    ``role_actions.py`` already applies to role changes -- applied immediately, not held for a
    confirmation step that exists to protect something this can never affect.
    """
    tcfg = cfg.terminals[terminal_id]
    key = account_key(tcfg.last_login, tcfg.last_server)
    if key is None:
        return "no account is known for this terminal yet; nothing to name"
    stripped = name.strip()
    if stripped and not ACCOUNT_NAME_RE.match(stripped):
        return ACCOUNT_NAME_RULE
    if stripped:
        cfg.account_names[key] = stripped
    else:
        cfg.account_names.pop(key, None)
    # The shared store (``shared_db``) is the source of truth: written per edit, one row
    # per account, so a rename in another profile's window is kept, not clobbered.
    from . import shared_data

    shared_data.set_name(key, stripped or None)
    return None


def _show(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items()) or "none"
    if isinstance(value, list):
        return ", ".join(value) or "none"
    if isinstance(value, str):
        return repr(value)
    return str(value)


def describe_changes(changes: tuple[Change, ...], limit: int | None = None) -> str:
    """One line for the log and the notification: ``risk_percent 1 -> 0.5, copy_pending off -> on``.

    The log gets it whole; a notification passes ``limit`` so a long symbol map cannot fill the
    screen. The audit trail is the one that must not be abbreviated.
    """
    text = ", ".join(f"{c.name} {_show(c.old)} -> {_show(c.new)}" for c in changes)
    if limit is not None and len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def advisories(outcome: EditOutcome) -> list[str]:
    """Warnings for an edit that is allowed but widens what a slave can lose or leaves it exposed.

    None is a reason to refuse. Each is something the editor makes one keypress away that was a
    deliberate hand edit before, so the moment it happens is the moment to say so.
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
