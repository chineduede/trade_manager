"""Prop-firm guards: rules, chosen per account, that measure how much harm the account may still
take. See ROADMAP.md, "Prop-firm guards", for the design.

Importing this package registers every rule.
"""

from . import rules
from .base import REGISTRY, Level
from .book import GuardBook, GuardReport, RuleStatus
from .model import AccountGuards, GuardRule, GuardsDoc

__all__ = [
    "REGISTRY",
    "AccountGuards",
    "GuardBook",
    "GuardReport",
    "GuardRule",
    "GuardsDoc",
    "Level",
    "RuleStatus",
    "rules",
]
