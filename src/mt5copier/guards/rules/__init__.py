"""The guard rules. Each module registers its rules on import; adding a rule is adding a
module here and naming it below."""

from . import class_lots, daily_loss, floating_loss, max_drawdown, news, per_trade, trade_counts

__all__ = [
    "class_lots",
    "daily_loss",
    "floating_loss",
    "max_drawdown",
    "news",
    "per_trade",
    "trade_counts",
]
