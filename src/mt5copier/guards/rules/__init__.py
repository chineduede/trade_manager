"""The guard rules. Each module registers its rules on import; adding a rule is adding a
module here and naming it below."""

from . import daily_loss, floating_loss, max_drawdown, per_trade

__all__ = ["daily_loss", "floating_loss", "max_drawdown", "per_trade"]
