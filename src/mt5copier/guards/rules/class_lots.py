"""A lots ceiling per asset class, because firms cap lot size by what is traded.

Two lots on forex, one on indices: the cap is a table, not a number, and which class a symbol
is in comes from the shared symbol catalog (:mod:`mt5copier.catalog`), never a firm's preset.
A ceiling of 0 leaves that class uncapped. Scope is the user's call: *each new trade on its
own*, or *all open lots of the class together, the new trade included* (the way Total margin
in use counts).

Like the margin caps, both scopes reduce to one number at dispatch -- the class's cap, or
what is left of it once the class's open lots are counted -- so no rule code or catalog
enters the worker: the engine sends a lots ceiling beside ``max_lot``, and the executor
clamps, refuses or warns as it already does for margin. The guard note always names the
class chosen ("US30 -> indices, capped at 1.00 lots"), so a misfiled symbol is visible,
never silent.
"""

from __future__ import annotations

from ...catalog import CLASS_WORDS, CLASSES
from ..account import AccountView
from ..base import Level, Measure, Memory, Rule, register
from ..params import Param, ParamKind, ParamValue, on_breach


@register
class ClassLots(Rule):
    type = "class_lots"
    title = "Max lots by asset class"
    blurb = "caps lot size per asset class"
    lots_cap = True
    params = (
        Param(
            "scope",
            "Counted as",
            ParamKind.CHOICE,
            "each",
            choices=(
                ("each", "each new trade on its own"),
                ("total", "all open lots of the class, the new trade included"),
            ),
        ),
        *(
            Param(
                clas,
                CLASS_WORDS[clas],
                ParamKind.NUMBER,
                0.0,
                at_least=0,
                after="lots · 0 = no cap",
            )
            for clas in CLASSES
        ),
        on_breach("shrink", "warn", "decline", "shrink"),
    )

    @staticmethod
    def caps(p: dict[str, ParamValue]) -> dict[str, float]:
        """The classes actually capped, in the order the classes are shown."""
        return {clas: float(p[clas]) for clas in CLASSES if float(p[clas]) > 0}

    def basis(self, p: dict[str, ParamValue]) -> str:
        return "a lots ceiling per asset class"

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        caps = self.caps(p)
        if not caps:
            return (
                "No class is capped yet: give at least one class a lots ceiling "
                "(0 leaves a class uncapped)."
            )
        listed = " · ".join(f"{CLASS_WORDS[c]} ≤ {v:g}" for c, v in caps.items())
        how = (
            "Each new trade on its own"
            if p["scope"] == "each"
            else "All open lots of a class together, a new trade included,"
        )
        return (
            f"{how} may be at most: {listed} lots. Which symbol is in which class is set "
            "in Settings, under Symbol catalog."
        )

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        caps = self.caps(p)
        if not caps:
            return Measure(level=Level.OK, detail="no class is capped yet (every ceiling is 0)")
        if p["scope"] == "each":
            listed = " · ".join(f"{CLASS_WORDS[c].lower()} ≤ {v:g}" for c, v in caps.items())
            return Measure(level=Level.OK, detail=f"{listed} lots a trade")
        if view.positions is None:
            return Measure(unknown="the open positions have not been read yet")
        if view.catalog is None:
            return Measure(unknown="the symbol catalog has not been read yet")
        open_lots = dict.fromkeys(caps, 0.0)
        for position in view.positions:
            clas = view.catalog.classify(view.base_symbol(position.symbol))
            if clas in open_lots:
                open_lots[clas] += position.volume
        worst = max(caps, key=lambda c: open_lots[c] / caps[c])
        listed = " · ".join(
            f"{CLASS_WORDS[c].lower()} {open_lots[c]:g} of {v:g} lots open" for c, v in caps.items()
        )
        return Measure(used=open_lots[worst], limit=caps[worst], detail=listed)
