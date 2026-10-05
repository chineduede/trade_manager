"""The engine's side of the guards: what they record as snapshots arrive, how each rule stands,
and what the rules together tell the engine to do about one slave -- its :class:`Verdict`.

A rule never acts itself. The verdict only says which of a few things the engine already knows
how to do applies now: hold the slave back from new trades, refuse a trade with no stop, cap a
new trade's risk or margin. Closing anything is not among them yet; a rule set to close copies
holds the slave back from new trades until it can.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from ..logging_setup import get_logger
from ..news import NewsCalendar
from ..paths import guard_state_path
from ..protocol import Snapshot
from .account import AccountView, last_reset
from .base import REGISTRY, SEVERITY, Level, Measure
from .model import GuardRule, GuardsDoc, write_json

log = get_logger(__name__)

_CONTINUITY_S = 60.0
"""The longest gap between two snapshots across which a reset still counts as watched. Longer,
and the equity seen after it is not the equity at the reset."""

_HOLDS_BACK = frozenset({"block_new", "close_worst", "flatten"})
"""What a rule may be set to do on breach that stops new trades. Closing copies comes in a
later stage; until then a rule set to close holds the slave back, the part of it already
built, rather than doing nothing."""


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
    """What it does when reached, in words."""
    act_at: float | None = None
    """The fraction of the limit at which the rule acts (its buffer), if it has one."""
    enabled: bool = True
    action: str = ""
    """What it does when reached: the ``on_breach`` value itself."""
    latched: str | None = None
    """When a breach is being held: ``"reset"`` until the next reset, ``"hand"`` until
    cleared. None when nothing is held."""

    @property
    def fraction(self) -> float | None:
        if self.used is None or not self.limit:
            return None
        return self.used / self.limit

    @property
    def acting(self) -> bool:
        """Past the point it acts at, or holding a breach that already did."""
        if not self.enabled:
            return False
        return self.latched is not None or self.level in (Level.NEAR, Level.BREACHED)

    @property
    def holds_back(self) -> bool:
        """Whether this rule is keeping the slave from new trades right now."""
        rule = REGISTRY.get(self.type)
        if rule is None or rule.per_trade():
            return False
        return self.acting and self.action in _HOLDS_BACK


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

    @property
    def holding_back(self) -> tuple[RuleStatus, ...]:
        return tuple(r for r in self.rules if r.holds_back)


@dataclass(frozen=True, slots=True)
class Cap:
    """A limit on one new trade, in the account's money."""

    amount: float
    mode: str
    """``"decline"``, ``"shrink"`` or ``"warn"``."""
    rule: str
    """The rule's title, for the log."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the guards tell the engine to do about one slave right now."""

    held_back: tuple[str, ...] = ()
    """Why the slave takes no new trades, one line per rule; empty when it takes them."""
    warnings: tuple[str, ...] = ()
    """Rules past their point that are set only to warn."""
    warned_by: tuple[str, ...] = ()
    """The rule behind each warning, in step: the lines carry live numbers, so the engine
    tells a new warning from the same one moving by its rule."""
    stop_required: str | None = None
    """The rule refusing new trades with no stop, when one is set to refuse."""
    stop_warning: str | None = None
    """The rule set only to warn about a new trade with no stop."""
    news: frozenset[str] = frozenset()
    """The currencies whose new trades a news rule declines now; ``"*"`` for every new trade."""
    news_why: str = ""
    """Which news, and until when, for the log."""
    max_risk: Cap | None = None
    max_margin: Cap | None = None

    @property
    def hold(self) -> str | None:
        return "; ".join(self.held_back) or None


NO_VERDICT = Verdict()


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
    def __init__(
        self,
        doc: GuardsDoc,
        state_path: Path | None = None,
        calendar: NewsCalendar | None = None,
    ) -> None:
        self.doc = doc
        self.calendar = calendar
        """The news calendar, asked only for an account with a news rule."""
        self._path = state_path if state_path is not None else guard_state_path()
        state = self._load()
        self._day_equity: dict[str, dict[str, dict[str, object]]] = _section(state, "day_equity")
        self._latched: dict[str, dict[str, dict[str, object]]] = _section(state, "latched")
        """Per account and rule: a breach being held, with ``until`` -- the reset day it lasts
        through, or ``"hand"`` -- and what it was when it tripped. On disk, so a restart does
        not lift a block the account earned."""
        self._seen: dict[str, float] = {}
        """Per account, when its previous snapshot arrived: what tells a reset that was watched
        from one that happened while the copier was off."""

    def _load(self) -> dict[str, object]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning("guard state at %s unreadable (%s); starting empty", self._path, exc)
            return {}

    def _save(self) -> None:
        try:
            write_json(self._path, {"day_equity": self._day_equity, "latched": self._latched})
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

    # --- latches ---------------------------------------------------------------------------

    def _latch_until(self, key: str, given: GuardRule, now: float) -> str | None:
        """The breach this rule is holding, if it still holds: ``"reset"`` or ``"hand"``.

        A breach is held against the rule as it was set when it tripped. Once the rule is
        changed -- a limit raised, say -- the hold is let go and the rule is measured afresh,
        tripping again at once if it is still past its point.
        """
        entry = self._latched.get(key, {}).get(given.id)
        if entry is None:
            return None
        params, _ = given.resolved()
        if "params" in entry and entry["params"] != params:
            return None
        until = entry.get("until")
        if until == "hand":
            return "hand"
        if "reset" in params and "zone" in params:
            _, day = last_reset(now, str(params["reset"]), str(params["zone"]))
            if until == day:
                return "reset"
        return None

    def _latch(self, key: str, given: GuardRule, status: RuleStatus, now: float) -> None:
        rule = REGISTRY.get(given.type)
        if rule is None or rule.latch is None:
            return
        until = "hand"
        if rule.latch == "reset":
            params, _ = given.resolved()
            _, until = last_reset(now, str(params["reset"]), str(params["zone"]))
        self._latched.setdefault(key, {})[given.id] = {
            "until": until,
            "at": now,
            "detail": status.detail,
            "params": given.resolved()[0],
        }
        log.warning(
            "guard %s on %s tripped and holds %s: %s",
            status.title,
            key,
            "until it is cleared on the guards page" if until == "hand" else "until the reset",
            status.detail,
        )
        self._save()

    def clear_latch(self, key: str, rule_id: str) -> bool:
        """Let go of a held breach by hand. False if there was none."""
        if self._latched.get(key, {}).pop(rule_id, None) is None:
            return False
        log.warning("guard breach on %s cleared by hand (rule %s)", key, rule_id)
        self._save()
        return True

    # --- reporting ------------------------------------------------------------------------

    def report(
        self, key: str | None, snap: Snapshot | None, now: float, sizing_risk: float | None = None
    ) -> GuardReport:
        guards = self.doc.for_account(key)
        currency = (snap.currency if snap else None) or ""
        wants_news = self.calendar is not None and any(
            r.enabled and getattr(REGISTRY.get(r.type), "news", False) for r in guards.rules
        )
        news = self.calendar.current() if wants_news and self.calendar is not None else None
        view = (
            AccountView.of(
                snap,
                now=now,
                initial_balance=guards.initial_balance,
                sizing_risk=sizing_risk,
                news=news,
            )
            if snap is not None
            else None
        )
        recall = _Recall(self, key or "")
        statuses = tuple(
            replace(
                _status(rule, guards.enabled, view, recall, currency),
                latched=self._latch_until(key, rule, now) if key else None,
            )
            for rule in guards.rules
        )
        return GuardReport(key, guards.enabled, guards.initial_balance, currency, statuses)

    def verdict(
        self, key: str | None, snap: Snapshot | None, now: float, sizing_risk: float | None = None
    ) -> Verdict:
        """What the guards tell the engine to do about this account now -- latching any breach
        that has just acted, so it holds even once the numbers recover."""
        if key is None:
            return NO_VERDICT
        report = self.report(key, snap, now, sizing_risk)
        if not report.enabled:
            return NO_VERDICT
        rules = {r.id: r for r in self.doc.for_account(key).rules}
        held: list[str] = []
        warnings: list[str] = []
        warned_by: list[str] = []
        stop_required = stop_warning = None
        news: set[str] = set()
        news_why: list[str] = []
        caps: dict[str, Cap] = {}
        for status in report.rules:
            rule = REGISTRY.get(status.type)
            if rule is None or not status.enabled:
                continue
            if rule.needs_stop:
                if status.action == "warn":
                    stop_warning = status.title
                else:
                    stop_required = status.title
                continue
            if rule.news:
                given = rules.get(status.rule_id)
                if given is None or self.calendar is None:
                    continue
                params, _ = given.resolved()
                on_news, why = rule.held(params, self.calendar.current(), now)  # type: ignore[attr-defined]
                if not on_news:
                    continue
                if status.action == "warn":
                    warnings.append(f"{status.title}: {why}")
                    warned_by.append(status.rule_id)
                else:
                    news |= on_news
                    news_why.append(why)
                continue
            if rule.cap is not None:
                if status.limit is not None:
                    cap = Cap(status.limit, status.action, status.title)
                    current = caps.get(rule.cap)
                    if current is None or cap.amount < current.amount:
                        caps[rule.cap] = cap
                continue
            if not status.acting:
                continue
            if status.latched is None and rule.latch is not None and status.action != "warn":
                given = rules.get(status.rule_id)
                if given is not None:
                    self._latch(key, given, status, now)
            line = _why(status)
            if status.action in _HOLDS_BACK:
                held.append(line)
            else:
                warnings.append(line)
                warned_by.append(status.rule_id)
        return Verdict(
            held_back=tuple(held),
            warnings=tuple(warnings),
            warned_by=tuple(warned_by),
            stop_required=stop_required,
            stop_warning=stop_warning,
            news=frozenset(news),
            news_why="; ".join(news_why),
            max_risk=caps.get("risk"),
            max_margin=caps.get("margin"),
        )


def _why(status: RuleStatus) -> str:
    """``Daily loss at 84% of its limit``, or that a breach is being held."""
    fraction = status.fraction
    said = status.title
    if fraction is not None:
        said += f" at {fraction:.0%} of its limit"
    if status.latched == "reset" and (fraction is None or fraction < (status.act_at or 1.0)):
        said += ", held until the reset"
    elif status.latched == "hand":
        said += ", held until cleared"
    return said


def _section(state: dict[str, object], name: str) -> dict[str, dict[str, dict[str, object]]]:
    value = state.get(name, {})
    return value if isinstance(value, dict) else {}


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
        action=str(params.get("on_breach", "")),
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
