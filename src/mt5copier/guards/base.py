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

    def opens_today(self, day: str) -> tuple[int, int]:
        """How many trades the copier placed on this account on ``day``, a local date from
        :func:`~mt5copier.guards.account.local_day`: market opens, then pending orders
        placed. ``(0, 0)`` for any other day."""
        ...


class Rule:
    type: ClassVar[str]
    title: ClassVar[str]
    blurb: ClassVar[str]
    """What the rule does, in a few words with no numbers, for the list a rule is added from."""
    params: ClassVar[tuple[Param, ...]]

    latch: ClassVar[str | None] = None
    """Whether a breach holds once it has acted: ``"reset"`` until the rule's next daily
    reset, ``"hand"`` until cleared on the guards page, None for a rule that measures a live
    state and lets go as soon as it recovers."""
    cap: ClassVar[str | None] = None
    """For a rule about one new trade: ``"risk"`` or ``"margin"``, the amount its limit caps.
    A rule with a cap is checked as each trade is sent, never against the account."""
    needs_stop: ClassVar[bool] = False
    """For a rule that refuses a new trade with no stop loss."""
    news: ClassVar[bool] = False
    """For the rule that refuses new trades around news, by the symbol's currency."""
    lots_cap: ClassVar[bool] = False
    """For the rule that caps a new trade's lots by its symbol's asset class. Checked as each
    trade is sent, like a ``cap`` rule, but the amount depends on the symbol, so the verdict
    carries the whole table and the engine reduces it to one number at dispatch."""
    counts: ClassVar[bool] = False
    """For a rule whose ``used`` and ``limit`` count trades rather than measure money: the
    page then shows ``2 trades a day``, never ``2.00 USD``."""

    @classmethod
    def per_trade(cls) -> bool:
        return cls.cap is not None or cls.needs_stop or cls.news or cls.lots_cap

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        """The rule as it is set, in words, for the guards page."""
        raise NotImplementedError

    def basis(self, p: dict[str, ParamValue]) -> str:
        """What its limit is taken of, in a few words, under the amount on the guards page:
        ``5% of the initial balance``. Empty for a rule with no amount."""
        return ""

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


def basis_words(p: dict[str, ParamValue], of: str) -> str:
    """``5% of the initial balance``, or ``a fixed amount`` for a limit set in money."""
    if p.get("unit") == "money":
        return "a fixed amount"
    return f"{float(p['limit']):g}% of {of}"


def amount_words(p: dict[str, ParamValue], of: str, currency: str) -> str:
    """``5% of the initial balance`` or ``500.00 USD``, from a rule's limit and unit."""
    limit = float(p["limit"])
    if p.get("unit") == "money":
        return money(limit, currency)
    return f"{limit:g}% of {of}"
