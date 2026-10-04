"""Rules about one new trade at a time: its margin, its risk, whether it has a stop.

These are checked as each trade is about to be sent, from the next stage on; until a trade
comes along, what they show is the limit they would hold it to right now.
"""

from __future__ import annotations

from ..account import AccountView
from ..base import Level, Measure, Memory, Rule, amount_words, money, register
from ..params import UNIT, Param, ParamKind, ParamValue, on_breach


@register
class MarginPerTrade(Rule):
    type = "margin_per_trade"
    title = "Margin per trade"
    params = (
        Param("limit", "Limit", ParamKind.NUMBER, 20.0, minimum=0),
        UNIT,
        on_breach("decline", "warn", "decline", "shrink"),
    )

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        return (
            f"A new trade may use at most {amount_words(p, 'the free margin', currency)} in margin."
        )

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        if p["unit"] == "money":
            limit = float(p["limit"])
        elif view.margin_free is None:
            return Measure(unknown="the free margin has not been read yet")
        else:
            limit = view.margin_free * float(p["limit"]) / 100
        return Measure(
            limit=limit,
            level=Level.OK,
            detail=f"a new trade may use up to {money(limit)} in margin now",
        )


@register
class RiskPerTrade(Rule):
    type = "risk_per_trade"
    title = "Risk per trade"
    params = (
        Param("limit", "Limit", ParamKind.NUMBER, 1.0, minimum=0),
        UNIT,
        Param(
            "percent_of",
            "A percent of",
            ParamKind.CHOICE,
            "initial_balance",
            choices=(("initial_balance", "the initial balance"), ("balance", "the balance")),
            only_if=("unit", "percent"),
        ),
        on_breach("decline", "warn", "decline", "shrink"),
    )

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        of = "the initial balance" if p["percent_of"] == "initial_balance" else "the balance"
        return f"A new trade may risk at most {amount_words(p, of, currency)} to its stop."

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        if p["unit"] == "money":
            limit = float(p["limit"])
        else:
            base = view.initial_balance if p["percent_of"] == "initial_balance" else view.balance
            if base is None:
                return Measure(
                    unknown="set the account's initial balance"
                    if p["percent_of"] == "initial_balance"
                    else "the balance has not been read yet"
                )
            limit = base * float(p["limit"]) / 100
        if view.sizing_risk is None:
            return Measure(limit=limit, level=Level.OK, detail=f"at most {money(limit)} a trade")
        # The slave's own sizing is the one risk known before any trade: if it already asks for
        # more than this rule allows, every trade it copies will be over.
        return Measure(
            used=view.sizing_risk,
            limit=limit,
            detail=f"this slave's sizing risks {money(view.sizing_risk)} a trade",
        )


@register
class StopRequired(Rule):
    type = "stop_required"
    title = "Stop loss required"
    params = (on_breach("decline", "warn", "decline"),)

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        return "Every new trade must carry a stop loss."

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        if view.positions is None:
            return Measure(unknown="the open positions have not been read yet")
        bare = sum(1 for position in view.positions if position.sl == 0.0)
        if bare == 0:
            return Measure(level=Level.OK, detail="every open position has a stop")
        word = "position has" if bare == 1 else "positions have"
        return Measure(level=Level.NEAR, detail=f"{bare} open {word} no stop")
