"""Maximum drawdown, static: how far equity may ever fall below the initial balance."""

from __future__ import annotations

from ..account import AccountView
from ..base import Measure, Memory, Rule, amount_words, basis_words, money, register
from ..params import BUFFER, UNIT, Param, ParamKind, ParamValue, on_breach


@register
class MaxDrawdown(Rule):
    type = "max_drawdown"
    title = "Max drawdown"
    blurb = "caps the fall below the initial balance"
    latch = "hand"
    params = (
        Param("limit", "Limit", ParamKind.NUMBER, 10.0, minimum=0),
        UNIT,
        BUFFER,
        on_breach("block_new", "warn", "block_new", "close_worst", "flatten"),
    )

    def basis(self, p: dict[str, ParamValue]) -> str:
        return basis_words(p, "the initial balance")

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        return (
            f"Never fall more than {amount_words(p, 'the initial balance', currency)} "
            "below the initial balance."
        )

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        if view.initial_balance is None:
            return Measure(unknown="set the account's initial balance")
        if view.equity is None:
            return Measure(unknown="the account's equity has not been read yet")
        start = view.initial_balance
        limit = float(p["limit"]) if p["unit"] == "money" else start * float(p["limit"]) / 100
        return Measure(
            used=max(0.0, start - view.equity),
            limit=limit,
            detail=f"stops at {money(start - limit)} · equity {money(view.equity)}",
        )
