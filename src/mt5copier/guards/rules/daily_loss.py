"""Daily loss: how far equity may fall below where the day started."""

from __future__ import annotations

from ..account import AccountView, last_reset
from ..base import Measure, Memory, Rule, amount_words, money, register
from ..params import BUFFER, UNIT, Param, ParamKind, ParamValue, on_breach


@register
class DailyLoss(Rule):
    type = "daily_loss"
    title = "Daily loss"
    params = (
        Param("limit", "Limit", ParamKind.NUMBER, 5.0, minimum=0),
        UNIT,
        Param(
            "percent_of",
            "A percent of",
            ParamKind.CHOICE,
            "initial_balance",
            choices=(
                ("initial_balance", "the initial balance"),
                ("day_start", "where the day started"),
            ),
        ),
        Param(
            "basis",
            "The day starts from",
            ParamKind.CHOICE,
            "balance",
            choices=(
                ("balance", "the balance at the reset"),
                ("equity", "the equity at the reset"),
                ("higher", "the higher of the two"),
            ),
        ),
        Param("reset", "The day resets at", ParamKind.TIME, "00:00"),
        Param("zone", "In the time zone", ParamKind.ZONE, "UTC"),
        BUFFER,
        on_breach("block_new", "warn", "block_new", "close_worst", "flatten"),
    )

    def sentence(self, p: dict[str, ParamValue], currency: str) -> str:
        of = "the initial balance" if p["percent_of"] == "initial_balance" else "the day's start"
        start = self.params[3].words(p["basis"])
        return (
            f"Lose at most {amount_words(p, of, currency)} in a day, from {start}; "
            f"the day resets at {p['reset']} {p['zone']}."
        )

    def measure(
        self, view: AccountView, p: dict[str, ParamValue], rule_id: str, memory: Memory
    ) -> Measure:
        if view.equity is None:
            return Measure(unknown="the account's equity has not been read yet")
        reset_at, day = last_reset(view.now, str(p["reset"]), str(p["zone"]))
        balance = view.balance_at(reset_at)
        equity = memory.day_start_equity(rule_id, day)
        basis = p["basis"]
        note = ""
        if basis == "balance":
            if balance is None:
                return Measure(unknown="today's trade history has not been read yet")
            start = balance
        elif basis == "equity":
            if equity is None:
                return Measure(
                    unknown="the equity at today's reset was not recorded: the copier was "
                    "not running then. It will be from the next reset."
                )
            start = equity
        else:
            if balance is None:
                return Measure(unknown="today's trade history has not been read yet")
            if equity is None:
                start = balance
                note = " (equity at the reset not recorded, so the balance is used)"
            else:
                start = max(balance, equity)

        if p["unit"] == "money":
            limit = float(p["limit"])
        else:
            base = start if p["percent_of"] == "day_start" else view.initial_balance
            if base is None:
                return Measure(unknown="set the account's initial balance")
            limit = base * float(p["limit"]) / 100
        loss = max(0.0, start - view.equity)
        return Measure(
            used=loss,
            limit=limit,
            detail=(
                f"day started at {money(start)}{note} · stops at {money(start - limit)} · "
                f"equity {money(view.equity)}"
            ),
        )
