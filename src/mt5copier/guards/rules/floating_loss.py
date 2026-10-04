"""Maximum floating loss: how much the open positions may be losing at once."""

from __future__ import annotations

from ..account import AccountView
from ..base import Measure, Memory, Rule, amount_words, money, register
from ..params import BUFFER, UNIT, Param, ParamKind, ParamValue, on_breach


@register
class FloatingLoss(Rule):
    type = "floating_loss"
    title = "Floating loss"
    params = (
        Param("limit", "Limit", ParamKind.NUMBER, 2.0, minimum=0),
        UNIT,
        Param(
            "percent_of",
            "A percent of",
            ParamKind.CHOICE,
            "initial_balance",
            choices=(("initial_balance", "the initial balance"), ("balance", "the balance")),
        ),
        Param(
            "counts",
            "Counting",
            ParamKind.CHOICE,
            "net",
            choices=(
                ("net", "the open trades' total"),
                ("losers", "only the losing trades"),
            ),
        ),
        BUFFER,
        on_breach("close_worst", "warn", "block_new", "close_worst", "flatten"),
    )

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        of = "the initial balance" if p["percent_of"] == "initial_balance" else "the balance"
        counting = "losing trades" if p["counts"] == "losers" else "open trades"
        return f"The {counting} may be down at most {amount_words(p, of, currency)} at once."

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        if view.positions is None:
            return Measure(unknown="the open positions have not been read yet")
        values: list[float] = []
        for position in view.positions:
            pnl = view.open_pnl(position.ticket)
            if pnl is None:
                return Measure(unknown=f"no floating P/L for #{position.ticket} yet")
            values.append(pnl)
        if p["counts"] == "losers":
            loss = -sum(v for v in values if v < 0)
        else:
            loss = max(0.0, -sum(values))

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
        open_count = len(view.positions)
        return Measure(
            used=loss,
            limit=limit,
            detail=f"{open_count} open · down {money(loss)}",
        )
