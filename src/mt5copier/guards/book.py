"""The engine's side of the guards: what they record as snapshots arrive, and how each rule
stands right now.

Display only in this stage: nothing here is read by the reconciler or the executor, and no
verdict is acted on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from ..logging_setup import get_logger
from ..paths import guard_state_path
from ..protocol import Snapshot
from .account import AccountView, last_reset
from .base import REGISTRY, SEVERITY, Level, Measure
from .model import GuardRule, GuardsDoc, write_json

log = get_logger(__name__)

_CONTINUITY_S = 60.0
"""The longest gap between two snapshots across which a reset still counts as watched. Longer,
and the equity seen after it is not the equity at the reset."""


@dataclass(frozen=True, slots=True)
class RuleStatus:
    rule_id: str
    type: str
    title: str
    sentence: str
    level: Level
    used: float | None = None
    limit: float | None = None
    detail: str = ""
    on_breach: str = ""
    act_at: float | None = None
    """The fraction of the limit at which the rule acts (its buffer), if it has one."""
    enabled: bool = True

    @property
    def fraction(self) -> float | None:
        if self.used is None or not self.limit:
            return None
        return self.used / self.limit


@dataclass(frozen=True, slots=True)
class GuardReport:
    account_key: str | None
    enabled: bool
    initial_balance: float | None
    currency: str
    rules: tuple[RuleStatus, ...]

    @property
    def worst(self) -> RuleStatus | None:
        """The rule standing most seriously, the fuller of two at the same level; None when no
        rule is measured."""
        live = [r for r in self.rules if r.level not in (Level.OFF, Level.UNKNOWN)]
        if not live:
            return None
        return max(live, key=lambda r: (SEVERITY[r.level], r.fraction or 0.0))


class _Recall:
    def __init__(self, book: GuardBook, key: str) -> None:
        self._book, self._key = book, key

    def day_start_equity(self, rule_id: str, day: str) -> float | None:
        entry = self._book._day_equity.get(self._key, {}).get(rule_id)
        if entry is None or entry.get("day") != day:
            return None
        equity = entry.get("equity")
        return float(equity) if isinstance(equity, int | float) else None


class GuardBook:
    def __init__(self, doc: GuardsDoc, state_path: Path | None = None) -> None:
        self.doc = doc
        self._path = state_path if state_path is not None else guard_state_path()
        self._day_equity: dict[str, dict[str, dict[str, object]]] = self._load()
        self._seen: dict[str, float] = {}
        """Per account, when its previous snapshot arrived: what tells a reset that was watched
        from one that happened while the copier was off."""

    def _load(self) -> dict[str, dict[str, dict[str, object]]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            day_equity = raw.get("day_equity", {})
            return day_equity if isinstance(day_equity, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning("guard state at %s unreadable (%s); starting empty", self._path, exc)
            return {}

    def _save(self) -> None:
        try:
            write_json(self._path, {"day_equity": self._day_equity})
        except OSError:
            log.exception("could not save guard state to %s", self._path)

    # --- recording ------------------------------------------------------------------------

    def observe(self, key: str | None, snap: Snapshot, now: float) -> None:
        """Note what only a running copier can see: the equity as a rule's day resets."""
        if key is None or snap.equity is None:
            return
        before = self._seen.get(key)
        self._seen[key] = now
        if before is None or now - before > _CONTINUITY_S:
            return
        changed = False
        for rule in self.doc.for_account(key).rules:
            params, problems = rule.resolved()
            if problems or "reset" not in params or "zone" not in params:
                continue
            reset_at, day = last_reset(now, str(params["reset"]), str(params["zone"]))
            if before < reset_at <= now:
                self._day_equity.setdefault(key, {})[rule.id] = {
                    "day": day,
                    "equity": snap.equity,
                }
                log.info("recorded equity %.2f at %s's daily reset (%s)", snap.equity, key, day)
                changed = True
        if changed:
            self._save()

    # --- reporting ------------------------------------------------------------------------

    def report(
        self, key: str | None, snap: Snapshot | None, now: float, sizing_risk: float | None = None
    ) -> GuardReport:
        guards = self.doc.for_account(key)
        currency = (snap.currency if snap else None) or ""
        view = (
            AccountView.of(
                snap, now=now, initial_balance=guards.initial_balance, sizing_risk=sizing_risk
            )
            if snap is not None
            else None
        )
        recall = _Recall(self, key or "")
        statuses = tuple(
            _status(rule, guards.enabled, view, recall, currency) for rule in guards.rules
        )
        return GuardReport(key, guards.enabled, guards.initial_balance, currency, statuses)


def _status(
    given: GuardRule, set_on: bool, view: AccountView | None, recall: _Recall, currency: str
) -> RuleStatus:
    enabled = given.enabled and set_on
    params, problems = given.resolved()
    rule = REGISTRY.get(given.type)
    if rule is None or problems:
        title = rule.title if rule else given.type
        return RuleStatus(
            given.id, given.type, title, "; ".join(problems), Level.UNKNOWN, enabled=enabled
        )
    breach = next((p for p in rule.params if p.name == "on_breach"), None)
    buffer = params.get("buffer_pct")
    status = RuleStatus(
        given.id,
        given.type,
        rule.title,
        rule.sentence(params, currency),
        Level.UNKNOWN,
        on_breach=breach.words(params["on_breach"]) if breach else "",
        act_at=float(buffer) / 100 if buffer is not None else None,
        enabled=enabled,
    )
    if not enabled:
        return replace(status, level=Level.OFF)
    if view is None:
        return replace(status, detail="not connected")
    try:
        found = rule.measure(view, params, given.id, recall)
    except Exception as exc:
        # A rule that fails must say so on its row, never take the page or the drain down.
        log.exception("guard rule %s (%s) failed", given.type, given.id)
        found = Measure(unknown=f"could not be measured: {exc}")
    if found.unknown is not None:
        return replace(status, detail=found.unknown)
    return replace(
        status,
        level=_level(found, status.act_at),
        used=found.used,
        limit=found.limit,
        detail=found.detail,
    )


def _level(found: Measure, act_at: float | None) -> Level:
    if found.level is not None:
        return found.level
    if found.used is None or found.limit is None:
        return Level.OK
    if found.used >= found.limit:
        return Level.BREACHED
    if act_at is not None and found.used >= found.limit * act_at:
        return Level.NEAR
    return Level.OK
