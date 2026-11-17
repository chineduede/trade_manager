"""What a guard rule can be set to: each rule declares its parameters, and everything else --
checking a value, filling a default, building the form that edits it -- is done from that
declaration, so a new rule brings no code of its own for any of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

ParamValue = float | str


class ParamKind(StrEnum):
    NUMBER = "number"
    CHOICE = "choice"
    TIME = "time"
    """A time of day, ``HH:MM`` on the 24-hour clock."""
    ZONE = "zone"
    """An IANA time zone name, such as ``Europe/Prague`` or ``America/New_York``, or a fixed
    offset from UTC in whole hours, such as ``UTC+2``."""


ZONE_HINT = (
    "A place, which follows its clocks' summer time: Europe/London, Europe/Prague, "
    "Europe/Athens (most brokers' server time), America/New_York. Or a fixed offset: UTC+2."
)
"""What the form says under a time zone field."""

_OFFSET = re.compile(r"(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)


@cache
def _names() -> dict[str, str]:
    """Every zone name the system knows, by its lower case, so ``europe/london`` is found."""
    return {name.lower(): name for name in available_timezones()}


def normal_zone(text: str) -> tuple[str | None, str | None]:
    """``(name, None)`` for a zone the copier can use, as it is written back -- ``UTC+2`` for an
    offset, the zone's own spelling for a name -- or ``(None, why)``."""
    text = text.strip()
    if text.upper() in ("UTC", "GMT", "Z", "UTC+0", "GMT+0", "UTC-0", "GMT-0"):
        return "UTC", None
    offset = _OFFSET.fullmatch(text)
    if offset is not None:
        sign, hours, minutes = offset[1], int(offset[2]), offset[3]
        if minutes not in (None, "00"):
            return None, (
                f"{text!r}: an offset must be whole hours; for a half-hour zone name the place, "
                "such as Asia/Kolkata"
            )
        if hours == 0:
            return "UTC", None
        if hours > (14 if sign == "+" else 12):
            return None, f"{text!r}: no place is that far from UTC"
        return f"UTC{sign}{hours}", None
    name = _names().get(text.lower())
    if name is None:
        return None, f"no time zone called {text!r} (try Europe/London, or UTC+2)"
    return name, None


def zone_info(text: str) -> ZoneInfo:
    """The zone a stored name stands for. ``UTC+2`` is ``Etc/GMT-2``: those names count west
    of Greenwich as positive, the reverse of how an offset is written everywhere else."""
    offset = _OFFSET.fullmatch(text.strip())
    if offset is not None:
        flipped = "-" if offset[1] == "+" else "+"
        return ZoneInfo(f"Etc/GMT{flipped}{int(offset[2])}")
    return ZoneInfo(text)


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    label: str
    kind: ParamKind
    default: ParamValue
    choices: tuple[tuple[str, str], ...] = ()
    """``(value, words)`` pairs for a CHOICE, in the order they are offered."""
    minimum: float | None = None
    """For a NUMBER: the value must be greater than this."""
    at_least: float | None = None
    """For a NUMBER: the value must be at least this -- inclusive, where ``minimum`` is not,
    for a field whose floor is itself meaningful (a cap where 0 means no cap)."""
    maximum: float | None = None
    """For a NUMBER: the value must be at most this."""
    after: str = ""
    """Words the form puts after the value, such as a unit."""
    only_if: tuple[str, str] | None = None
    """``(other, value)``: this parameter means something only while ``other`` is ``value``,
    and the form hides it otherwise -- what a percent is of, while the limit is in money."""

    def check(self, value: object) -> tuple[ParamValue | None, str | None]:
        """``(value, None)`` when acceptable, normalised; ``(None, why)`` when not."""
        if self.kind is ParamKind.NUMBER:
            try:
                number = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None, f"{self.label} must be a number"
            if number != number or number in (float("inf"), float("-inf")):
                return None, f"{self.label} must be a number"
            if self.minimum is not None and number <= self.minimum:
                return None, f"{self.label} must be more than {self.minimum:g}"
            if self.at_least is not None and number < self.at_least:
                return None, f"{self.label} must be at least {self.at_least:g}"
            if self.maximum is not None and number > self.maximum:
                return None, f"{self.label} must be at most {self.maximum:g}"
            return number, None
        text = str(value).strip()
        if self.kind is ParamKind.CHOICE:
            if text not in {v for v, _ in self.choices}:
                return None, f"{self.label} must be one of {', '.join(v for v, _ in self.choices)}"
            return text, None
        if self.kind is ParamKind.TIME:
            match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
            if match is None or int(match[1]) > 23 or int(match[2]) > 59:
                return None, f"{self.label} must be a time like 00:00 or 17:00"
            return f"{int(match[1]):02d}:{match[2]}", None
        name, why = normal_zone(text)
        if name is None:
            return None, f"{self.label}: {why}"
        try:
            zone_info(name)
        except (ZoneInfoNotFoundError, ValueError):
            return None, f"{self.label}: no time zone called {text!r} (try Europe/London, or UTC+2)"
        return name, None

    def words(self, value: ParamValue) -> str:
        """How a CHOICE's value reads; anything else as written."""
        for choice, words in self.choices:
            if choice == value:
                return words
        return f"{value:g}" if isinstance(value, float) else str(value)


def resolve(
    params: tuple[Param, ...], given: dict[str, ParamValue]
) -> tuple[dict[str, ParamValue], list[str]]:
    """Every declared parameter's value -- the given one where it checks, else the default --
    and the problems found. A value that does not check is reported, never silently used."""
    out: dict[str, ParamValue] = {}
    problems: list[str] = []
    for param in params:
        if param.name not in given:
            out[param.name] = param.default
            continue
        value, problem = param.check(given[param.name])
        if problem is not None:
            problems.append(problem)
            out[param.name] = param.default
        else:
            assert value is not None
            out[param.name] = value
    return out, problems


# --- parameters many rules share -------------------------------------------------------------

BUFFER = Param(
    "buffer_pct",
    "Act at",
    ParamKind.NUMBER,
    80.0,
    minimum=0,
    maximum=100,
    after="% of the limit",
)
"""How much of the limit may be used before the rule acts, in percent. The firm checks every
tick on its own server; the copier sees a snapshot a few times a second, nothing while it is
off, and a gap can jump any line -- so acting at the limit itself is acting too late."""

UNIT = Param(
    "unit",
    "Measured in",
    ParamKind.CHOICE,
    "percent",
    choices=(("percent", "percent"), ("money", "money")),
)


def on_breach(default: str, *choices: str) -> Param:
    words = {
        "warn": "only warn",
        "decline": "decline the trade",
        "shrink": "shrink the trade to fit",
        "block_new": "take no new trades",
        "close_worst": "close the worst copy",
        "flatten": "close every copy",
    }
    return Param(
        "on_breach",
        "When it is reached",
        ParamKind.CHOICE,
        default,
        choices=tuple((c, words[c]) for c in choices),
    )
