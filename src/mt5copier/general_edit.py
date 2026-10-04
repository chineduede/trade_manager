"""The logic behind the app-wide settings form, with no UI in it.

The settings here belong to the copier rather than to one terminal: whether it sends real
orders, how its connections are timed, and how long a lapse in watching the master still counts
as unbroken, and whether it closes the terminals it is done with. As in :mod:`mt5copier.settings_edit`, the bounds are the model's own, so the form
cannot let through something the next start would read as a corrupt config.

**The mode is guarded twice.** Changing it in either direction needs copying stopped: going
live with the diff running would send orders the moment the save lands, and going back to a
dry run would leave live copies unmanaged mid-trade. Going live also needs ``LIVE`` typed out,
which :func:`apply_general` asks for by setting :attr:`GeneralOutcome.confirm_live` rather than
by refusing, so the form can open the question and then save again with the answer.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from .config import AppConfig
from .settings_edit import Change

DRY_RUN = "dry_run"
LIVE_WORD = "LIVE"

GENERAL: tuple[str, ...] = (
    DRY_RUN,
    "poll_interval_ms",
    "heartbeat_timeout_s",
    "reconnect_backoff_s",
    "join_grace_seconds",
    "close_terminals",
)
"""The :class:`AppConfig` fields the form edits. ``copying_enabled`` is not one: it has its own
key, ``d``, and is flipped far more often than anything here."""

APPLIES_ON_CONNECT = ("poll_interval_ms", "reconnect_backoff_s")
"""Handed to each terminal's worker when it starts, so a change reaches a terminal the next
time its connection starts, not at once."""

HELP: dict[str, tuple[str, str]] = {
    DRY_RUN: (
        "Mode",
        "A dry run works out every copy and logs it, and sends nothing. Live sends real orders "
        "to every connected slave the demo-only guard lets through.",
    ),
    "poll_interval_ms": (
        "Check interval",
        "How often each terminal's account, positions and orders are read, in milliseconds. "
        "Lower notices the master sooner and works the terminals harder. 50 to 10000.",
    ),
    "heartbeat_timeout_s": (
        "Stale after",
        "A terminal that has sent nothing for this long is shown as stale: the copier's own "
        "link to it has stuck, as opposed to the terminal losing its broker.",
    ),
    "reconnect_backoff_s": (
        "Retry schedule",
        "Seconds to wait before each attempt to reconnect a lost terminal, separated by commas. "
        "The last one repeats for as long as it takes.",
    ),
    "close_terminals": (
        "Closing terminals",
        "What happens to a terminal's window when the copier quits or the terminal is "
        "unassigned. A restart or reconnect never closes one. Closing is immediate, not "
        "MT5's own exit: anything unsaved in that terminal, such as a chart, is lost.",
    ),
    "join_grace_seconds": (
        "Join grace",
        "A gap this short in watching the master is treated as unbroken. After a longer one, "
        "trades the master already holds are never copied to that slave, because a late copy "
        "fills far from the master's entry. 0 to 5.",
    ),
}


@dataclass(frozen=True)
class GeneralOutcome:
    values: dict[str, object] | None
    """Every edited field's new value, or None if anything was wrong or a question is owed."""

    errors: dict[str, str]
    changes: tuple[Change, ...] = ()
    confirm_live: bool = False
    """Everything else is in order, and the edit goes live: ask for ``LIVE`` and save again."""

    @property
    def ok(self) -> bool:
        return self.values is not None


def _number(value: float) -> str:
    return format(value, ".15g")


def initial_general(cfg: AppConfig) -> dict[str, str | bool]:
    """The form's starting values. The mode is ``True`` for a dry run, as the field is."""
    return {
        DRY_RUN: cfg.dry_run,
        "poll_interval_ms": str(cfg.poll_interval_ms),
        "heartbeat_timeout_s": _number(cfg.heartbeat_timeout_s),
        "reconnect_backoff_s": ", ".join(_number(s) for s in cfg.reconnect_backoff_s),
        "join_grace_seconds": _number(cfg.join_grace_seconds),
        "close_terminals": cfg.close_terminals.value,
    }


class _EntryError(Exception):
    pass


_SEPARATORS = re.compile(r"[\s,;]+")


def _float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        hint = " (use a dot for decimals)" if "," in text else ""
        raise _EntryError(f"not a number{hint}") from None
    if not math.isfinite(value):
        raise _EntryError("must be a finite number")
    return value


def _parse(name: str, raw: str | bool) -> object:
    if name == DRY_RUN:
        if not isinstance(raw, bool):
            raise _EntryError("expected dry run or live")
        return raw
    if not isinstance(raw, str):
        raise _EntryError("expected text")
    text = raw.strip()
    if name == "close_terminals":
        return text
    if name == "reconnect_backoff_s":
        steps = [_float(t) for t in _SEPARATORS.split(text) if t]
        if not steps:
            raise _EntryError("give at least one wait")
        if any(s <= 0 for s in steps):
            raise _EntryError("every wait must be more than 0")
        return steps
    if text == "":
        raise _EntryError("required")
    if name == "poll_interval_ms":
        try:
            return int(text)
        except ValueError:
            raise _EntryError("must be a whole number") from None
    return _float(text)


_BOUNDS = {
    "greater_than": "greater than",
    "greater_than_equal": "at least",
    "less_than": "less than",
    "less_than_equal": "at most",
}


def _message(err: Mapping[str, object]) -> str:
    kind = str(err.get("type"))
    ctx = err.get("ctx")
    if kind in _BOUNDS and isinstance(ctx, Mapping) and ctx:
        limit = next(iter(ctx.values()))
        shown = _number(limit) if isinstance(limit, int | float) else limit
        return f"must be {_BOUNDS[kind]} {shown}"
    return str(err["msg"])


def apply_general(
    cfg: AppConfig, raw: Mapping[str, str | bool], *, typed: str | None = None
) -> GeneralOutcome:
    """Check what the form holds against ``cfg``, the live config at the moment of saving.

    ``typed`` is what was typed to confirm going live; None is "not asked yet", and an edit
    that goes live without it comes back with :attr:`~GeneralOutcome.confirm_live` set.
    Nothing is changed here: :func:`commit_general` does that.
    """
    unknown = sorted(set(raw) - set(GENERAL))
    if unknown:
        raise ValueError(f"not editable here: {', '.join(unknown)}")

    start = initial_general(cfg)
    updates: dict[str, object] = {}
    errors: dict[str, str] = {}
    for name, value in raw.items():
        if value == start[name]:
            continue
        try:
            updates[name] = _parse(name, value)
        except _EntryError as exc:
            errors[name] = str(exc)

    try:
        checked = AppConfig.model_validate({**cfg.model_dump(), **updates})
    except ValidationError as exc:
        checked = None
        for err in exc.errors():
            key = str(err["loc"][0]) if err["loc"] else ""
            errors.setdefault(key if key in GENERAL else "", _message(err))

    going = updates.get(DRY_RUN)
    if going is not None and going != cfg.dry_run and cfg.copying_enabled:
        errors[DRY_RUN] = "copying is running: stop it with d first, then change the mode"

    if errors or checked is None:
        return GeneralOutcome(values=None, errors=errors)

    changes = tuple(
        Change(name, getattr(cfg, name), getattr(checked, name))
        for name in updates
        if getattr(cfg, name) != getattr(checked, name)
    )
    if any(c.name == DRY_RUN and c.new is False for c in changes):
        if typed is None:
            return GeneralOutcome(values=None, errors={}, changes=changes, confirm_live=True)
        if typed.strip().upper() != LIVE_WORD:
            return GeneralOutcome(
                values=None, errors={DRY_RUN: f"that is not {LIVE_WORD}: nothing was changed"}
            )
    return GeneralOutcome(values={c.name: c.new for c in changes}, errors={}, changes=changes)


def commit_general(cfg: AppConfig, outcome: GeneralOutcome) -> None:
    """Write a checked edit into the live config. The caller saves it and syncs the engine."""
    if outcome.values is None:
        raise ValueError("nothing to commit: the edit was not checked, or was refused")
    for name, value in outcome.values.items():
        setattr(cfg, name, value)
