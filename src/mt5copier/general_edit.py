"""The logic behind the app-wide settings form, with no UI in it.

The settings here belong to the copier rather than to one terminal: whether it sends real
orders, how its connections are timed, and how long a lapse in watching the master still counts
as unbroken, whether it closes the terminals it is done with, and how long it keeps
history. As in :mod:`mt5copier.settings_edit`, the bounds are the model's own, so the form
cannot let through something the next start would read as a corrupt config.

**The mode is guarded twice.** Changing it in either direction needs copying stopped: going
live with the diff running would send orders the moment the save lands, and going back to a
dry run would leave live copies unmanaged mid-trade. Going live also needs ``LIVE`` typed out,
which :func:`apply_general` asks for by setting :attr:`GeneralOutcome.confirm_live` rather than
by refusing, so the form can open the question and then save again with the answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from . import shared_data
from .config import AppConfig
from .settings_edit import _SEPARATORS, Change, _EntryError, _float, _message, _number

DRY_RUN = "dry_run"
LIVE_WORD = "LIVE"

GENERAL: tuple[str, ...] = (
    DRY_RUN,
    "poll_interval_ms",
    "heartbeat_timeout_s",
    "reconnect_backoff_s",
    "rescan_interval_s",
    "join_grace_seconds",
    "close_terminals",
    "history_days",
)
"""The :class:`AppConfig` fields the form edits. ``copying_enabled`` is not one: it has its own
key, ``d``, and is flipped far more often than anything here."""

APPLIES_ON_CONNECT = ("poll_interval_ms", "reconnect_backoff_s")
"""Handed to each terminal's worker when it starts, so a change reaches a terminal the next
time its connection starts, not at once."""


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


def initial_general(cfg: AppConfig) -> dict[str, str | bool]:
    """The form's starting values. The mode is ``True`` for a dry run, as the field is."""
    return {
        DRY_RUN: cfg.dry_run,
        "poll_interval_ms": str(cfg.poll_interval_ms),
        "heartbeat_timeout_s": _number(cfg.heartbeat_timeout_s),
        "reconnect_backoff_s": ", ".join(_number(s) for s in cfg.reconnect_backoff_s),
        "rescan_interval_s": _number(cfg.rescan_interval_s),
        "join_grace_seconds": _number(cfg.join_grace_seconds),
        "close_terminals": cfg.close_terminals.value,
        "history_days": str(cfg.history_days),
    }


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
    if name in ("poll_interval_ms", "history_days"):
        try:
            return int(text)
        except ValueError:
            raise _EntryError("must be a whole number") from None
    return _float(text)


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
    if any(name in shared_data.MACHINE_FIELDS for name in outcome.values):
        # The timings describe the machine, so every profile shares them: written only when
        # this edit touched one, so a window open since before another profile changed them
        # cannot quietly put the old values back by saving something unrelated.
        shared_data.save_machine(cfg)
