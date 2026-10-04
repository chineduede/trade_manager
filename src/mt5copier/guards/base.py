"""The shape every guard rule has, and the registry that finds one by its type name.

A rule is one class in :mod:`mt5copier.guards.rules`: a type name, a title, the parameters it
takes, the sentence it reads as, and :meth:`Rule.measure` -- how much of its limit the account
has used. Nothing else. The book turns a measurement into a level, the page draws it, and (from
the next stage) the engine acts on it: a rule never sends anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

from .account import AccountView
from .params import Param, ParamValue


class Level(StrEnum):
    """How a rule stands, least to most serious."""

    OFF = "off"
    UNKNOWN = "unknown"
    OK = "ok"
    NEAR = "near"
    """Past the rule's buffer: where it will act."""
    BREACHED = "breached"
    """At or past the limit itself."""


SEVERITY = {level: n for n, level in enumerate(Level)}


@dataclass(frozen=True, slots=True)
class Measure:
    """What a rule found. ``used`` and ``limit`` are money, in the account's currency."""

    used: float | None = None
    limit: float | None = None
    detail: str = ""
    unknown: str | None = None
    """Why it cannot be measured right now, when it cannot."""
    level: Level | None = None
    """Set by a rule whose standing is not a fraction of a limit -- a count, a yes or no."""


class Memory(Protocol):
    """What a rule may recall from earlier snapshots."""

    def day_start_equity(self, rule_id: str, day: str) -> float | None: ...


class Rule:
    type: ClassVar[str]
    title: ClassVar[str]
    params: ClassVar[tuple[Param, ...]]

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        """The rule as it is set, in words, for the guards page."""
        raise NotImplementedError

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        raise NotImplementedError


REGISTRY: dict[str, Rule] = {}


def register[R: type[Rule]](cls: R) -> R:
    if cls.type in REGISTRY:
        raise ValueError(f"two guard rules are called {cls.type!r}")
    REGISTRY[cls.type] = cls()
    return cls


def money(amount: float, currency: str = "") -> str:
    """Money as the rest of the app shows it: thousands commas, two places."""
    text = f"{amount:,.2f}"
    return f"{text} {currency}".rstrip()


def amount_words(p: dict[str, ParamValue], of: str, currency: str) -> str:
    """``5% of the initial balance`` or ``500.00 USD``, from a rule's limit and unit."""
    limit = float(p["limit"])
    if p.get("unit") == "money":
        return money(limit, currency)
    return f"{limit:g}% of {of}"
