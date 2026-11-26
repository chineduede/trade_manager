"""The engine's side of the guards: what they record as snapshots arrive, how each rule stands,
and what the rules together tell the engine to do about one slave -- its :class:`Verdict`.

A rule never acts itself. The verdict only says which of a few things the engine already knows
how to do applies now: hold the slave back from new trades, refuse a trade with no stop, cap a
new trade's risk or margin, close the worst copy or every copy.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .. import shared_db
from ..catalog import CLASSES, CatalogStore
from ..logging_setup import get_logger
from ..news import NewsCalendar
from ..prop_accounts import PropStore
from ..protocol import Snapshot
from ..shared_db import SharedDb
from .account import AccountView, last_reset, local_day
from .base import REGISTRY, SEVERITY, Level, Measure
from .model import GuardRule, GuardsDoc
from .rules.news import acting

log = get_logger(__name__)

_CONTINUITY_S = 60.0
"""The longest gap between two snapshots across which a reset still counts as watched. Longer,
and the equity seen after it is not the equity at the reset."""

_HOLDS_BACK = frozenset({"block_new", "close_worst", "flatten"})
"""What a rule may be set to do on breach that stops new trades. A rule set to close copies
holds the slave back too: it takes no new trades while old ones are being closed."""

_CLOSES = frozenset({"close_worst", "flatten"})
"""What a rule may be set to do on breach that closes open copies."""


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
    basis: str = ""
    """What its limit is taken of, in a few words: ``30% of the free margin``."""
    per_trade: bool = False
    """A rule about each new trade, rather than about the account as it stands."""

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
    rule_id: str = ""


@dataclass(frozen=True, slots=True)
class ClassLotsCap:
    """One rule's lots ceilings per asset class. The amount a trade is held to depends on its
    symbol, so the verdict carries the table and the engine reduces it to one number at
    dispatch -- the class's cap, or for the ``total`` scope what is left of it once the
    class's open lots are counted off the slave's snapshot."""

    rule_id: str
    title: str
    mode: str
    """``"decline"``, ``"shrink"`` or ``"warn"``."""
    scope: str
    """``"each"`` -- each new trade on its own -- or ``"total"`` -- all open lots of the
    class together, the new trade included."""
    caps: tuple[tuple[str, float], ...]
    """``(class, lots)`` for every class actually capped."""

    def cap_for(self, clas: str) -> float | None:
        return next((lots for c, lots in self.caps if c == clas), None)


@dataclass(frozen=True, slots=True)
class NewsAct:
    """A news rule acting on the copies the slave holds now, before a release."""

    rule_id: str
    title: str
    mode: str
    """``"breakeven"`` or ``"close"``."""
    currencies: frozenset[str]
    """The news currencies; ``"*"`` for every copy."""
    why: str
    """Which news, and when."""


@dataclass(frozen=True, slots=True)
class CloseAct:
    """An account rule closing open copies now: the worst one at a time, or every one."""

    rule_id: str
    title: str
    mode: str
    """``"close_worst"`` or ``"flatten"``."""
    why: str
    """How the rule stands: ``at 92% of its limit``."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the guards tell the engine to do about one slave right now."""

    held_back: tuple[str, ...] = ()
    """Why the slave takes no new trades, one line per rule; empty when it takes them."""
    held_by: tuple[str, ...] = ()
    """The rule behind each line of ``held_back``, in step."""
    warnings: tuple[str, ...] = ()
    """Rules past their point that are set only to warn."""
    warned_by: tuple[str, ...] = ()
    """The rule behind each warning, in step: the lines carry live numbers, so the engine
    tells a new warning from the same one moving by its rule."""
    stop_required: str | None = None
    """The rule refusing new trades with no stop, when one is set to refuse."""
    stop_warning: str | None = None
    """The rule set only to warn about a new trade with no stop."""
    stop_warned_by: str = ""
    """The rule behind ``stop_warning``."""
    news: frozenset[str] = frozenset()
    """The currencies whose new trades a news rule declines now; ``"*"`` for every new trade."""
    news_why: str = ""
    """Which news, and until when, for the log."""
    news_by: tuple[str, ...] = ()
    """The news rules declining trades now."""
    stop_by: str = ""
    """The rule behind ``stop_required``."""
    max_risk: Cap | None = None
    max_margin: Cap | None = None
    max_total_margin: Cap | None = None
    """The most margin every open trade and a new one may use together."""
    class_lots: tuple[ClassLotsCap, ...] = ()
    """Lots ceilings per asset class, one entry per rule; the engine applies the tightest."""
    news_acts: tuple[NewsAct, ...] = ()
    """News rules acting on open copies now, before a release."""
    close_acts: tuple[CloseAct, ...] = ()
    """Account rules set to close copies that are past the point they act at now."""

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

    def opens_today(self, day: str) -> tuple[int, int]:
        entry = self._book._opens.get(self._key)
        if entry is None or entry.get("day") != day:
            return (0, 0)
        market, pending = entry.get("market"), entry.get("pending")
        return (
            market if isinstance(market, int) else 0,
            pending if isinstance(pending, int) else 0,
        )


class GuardBook:
    def __init__(
        self,
        doc: GuardsDoc,
        db: SharedDb | None = None,
        calendar: NewsCalendar | None = None,
        catalog: CatalogStore | None = None,
        props: PropStore | None = None,
    ) -> None:
        self.doc = doc
        self.calendar = calendar
        """The news calendar, asked only for an account with a news rule."""
        self.catalog = catalog
        """The symbol catalog, read only for an account with a rule about asset classes."""
        self.props = props
        """The prop account containers. An account linked into one reads its *rules* under
        the container's identity (:meth:`_rules_key`); everything stateful in this book --
        latches, day-start equity, trade counts -- stays keyed by the raw account, so a
        closed stage's breach can never hold back the stage that replaced it."""
        self._db = db if db is not None else shared_db.get()
        state = self._load()
        self._day_equity: dict[str, dict[str, dict[str, object]]] = _section(state, "day_equity")
        self._latched: dict[str, dict[str, dict[str, object]]] = _section(state, "latched")
        """Per account and rule: a breach being held, with ``until`` -- the reset day it lasts
        through, or ``"hand"`` -- and what it was when it tripped. On disk, so a restart does
        not lift a block the account earned."""
        self._opens: dict[str, dict[str, object]] = _section(state, "opens")
        """Per account: how many trades the copier placed on it today -- ``day`` (a local
        date), ``market`` and ``pending`` counts -- for the trade-count rules. On disk, so a
        restart does not hand back a day's worth of trades."""
        self._seen: dict[str, float] = {}
        """Per account, when its previous snapshot arrived: what tells a reset that was watched
        from one that happened while the copier was off."""
        self._dirty: set[str] = set()
        """The accounts whose state THIS process has changed. The store is shared by every
        profile, and claims mean no two profiles guard one account at once -- so a save
        writes only these keys' rows, leaving every other account's exactly as the profile
        that owns them wrote them."""

    def _load(self) -> dict[str, dict[str, Any]]:
        """The whole store regrouped into sections keyed by account -- the shape this book
        works in; on disk it is a row per account holding that account's sections."""
        try:
            rows = self._db.load("guard_state")
        except sqlite3.Error:
            log.exception("could not read the guard state; starting empty")
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, row in rows.items():
            if not isinstance(row, dict):
                continue
            for name, held in row.items():
                out.setdefault(name, {})[key] = held
        return out

    def _rules_key(self, key: str | None) -> str | None:
        """The identity this account's *rules* are read under: its prop container's, when
        it is linked into one. Used exactly where ``self.doc`` is consulted, and nowhere a
        state section is keyed."""
        if self.props is None:
            return key
        return self.props.current().rules_key(key)

    def _sections(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        """Every per-account section the state file holds, for saving and adopting alike."""
        return (
            ("day_equity", self._day_equity),
            ("latched", self._latched),
            ("opens", self._opens),
        )

    def _save(self) -> None:
        """One transaction writing only the dirty accounts' rows (see ``_dirty``), so two
        profiles each guarding their own accounts never erase one another's anchors or held
        breaches -- not even at midnight, when both engines cross the same daily reset and
        save within the same second. A database error lands in the catch like any failed
        write; the keys stay dirty, so the next save carries them."""
        try:
            with self._db.change(shared_db.GUARD_STATE):
                for key in self._dirty:
                    row = {name: ours[key] for name, ours in self._sections() if key in ours}
                    if row:
                        self._db.put("guard_state", key, row)
                    else:
                        self._db.drop("guard_state", key)
        except (OSError, sqlite3.Error):
            log.exception("could not save the guard state")

    def _adopt(self, key: str) -> None:
        """Take one account's state from the store, the first time this process sees the
        account.

        The account may have been guarded from another profile until moments ago (a claim
        released, and this profile picked the terminal up): its anchors and held breaches are
        in the store, newer than what this book loaded at start. Never over our own changes --
        a dirty key is ours, and claims say nobody else has written it since."""
        if key in self._dirty:
            return
        try:
            row = self._db.fetch("guard_state", key)
        except sqlite3.Error:
            log.exception("could not read the guard state for %s; keeping what is held", key)
            return
        row = row if isinstance(row, dict) else {}
        for name, ours in self._sections():
            theirs = row.get(name)
            if theirs is not None:
                ours[key] = theirs
            else:
                ours.pop(key, None)

    # --- recording ------------------------------------------------------------------------

    def observe(self, key: str | None, snap: Snapshot, now: float) -> None:
        """Note what only a running copier can see: the equity as a rule's day resets."""
        if key is None or snap.equity is None:
            return
        before = self._seen.get(key)
        self._seen[key] = now
        if before is None:
            # First snapshot for this account since start: it may have been guarded from
            # another profile until moments ago, so its state on disk is newer than ours.
            self._adopt(key)
        if before is None or now - before > _CONTINUITY_S:
            return
        changed = False
        for rule in self.doc.for_account(self._rules_key(key)).rules:
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
            self._dirty.add(key)
            self._save()

    def record_open(self, key: str | None, *, pending: bool, now: float) -> None:
        """Count one trade the copier placed on this account -- a filled market order, or a
        pending order that rested -- for the trade-count rules.

        Counted whether or not such a rule is set, so a rule added at noon knows the
        morning; what it costs is one small write per trade placed. The day is this
        computer's (:func:`~mt5copier.guards.account.local_day`), and a stale entry rolls
        over here rather than on a timer: the first trade of a new day resets the counts.
        """
        if key is None:
            return
        self._adopt(key)
        day = local_day(now)
        entry = self._opens.get(key)
        if entry is None or entry.get("day") != day:
            entry = {"day": day, "market": 0, "pending": 0}
            self._opens[key] = entry
        kind = "pending" if pending else "market"
        count = entry.get(kind)
        entry[kind] = (count if isinstance(count, int) else 0) + 1
        self._dirty.add(key)
        self._save()

    def record_fill(self, key: str | None, *, now: float) -> None:
        """A resting copy became a position at its broker: move it from the pending column
        to the market one, so a rule that ignores pendings still counts the trade it became.

        Counted on the fill's own day. An order placed yesterday that fills today rolls the
        entry over and counts as today's market trade -- its placement spent yesterday's
        column, but the trade the firm sees happened today. Same-day, the totals are
        unchanged for a rule that counts pendings, so nothing is ever counted twice.
        """
        if key is None:
            return
        self._adopt(key)
        day = local_day(now)
        entry = self._opens.get(key)
        if entry is None or entry.get("day") != day:
            entry = {"day": day, "market": 0, "pending": 0}
            self._opens[key] = entry
        pending = entry.get("pending")
        if isinstance(pending, int) and pending > 0:
            entry["pending"] = pending - 1
        market = entry.get("market")
        entry["market"] = (market if isinstance(market, int) else 0) + 1
        self._dirty.add(key)
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
        self._dirty.add(key)
        self._save()

    def title(self, key: str | None, rule_id: str) -> str:
        """A rule's title, by its id on this account; ``a guard`` once it is gone."""
        for given in self.doc.for_account(self._rules_key(key)).rules:
            if given.id == rule_id:
                rule = REGISTRY.get(given.type)
                return rule.title if rule is not None else given.type
        return "A guard"

    def clear_latch(self, key: str, rule_id: str) -> bool:
        """Let go of a held breach by hand. False if there was none."""
        if self._latched.get(key, {}).pop(rule_id, None) is None:
            return False
        log.warning("guard breach on %s cleared by hand (rule %s)", key, rule_id)
        self._dirty.add(key)
        self._save()
        return True

    # --- reporting ------------------------------------------------------------------------

    def report(
        self,
        key: str | None,
        snap: Snapshot | None,
        now: float,
        sizing_risk: float | None = None,
        decor: tuple[str, str, tuple[tuple[str, str], ...]] | None = None,
    ) -> GuardReport:
        guards = self.doc.for_account(self._rules_key(key))
        currency = (snap.currency if snap else None) or ""
        wants_news = self.calendar is not None and any(
            r.enabled and getattr(REGISTRY.get(r.type), "news", False) for r in guards.rules
        )
        news = self.calendar.current() if wants_news and self.calendar is not None else None
        wants_classes = self.catalog is not None and any(
            r.enabled and getattr(REGISTRY.get(r.type), "lots_cap", False) for r in guards.rules
        )
        catalog = self.catalog.current() if wants_classes and self.catalog is not None else None
        view = (
            AccountView.of(
                snap,
                now=now,
                initial_balance=guards.initial_balance,
                sizing_risk=sizing_risk,
                news=news,
                catalog=catalog,
                decor=decor,
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
        self,
        key: str | None,
        snap: Snapshot | None,
        now: float,
        sizing_risk: float | None = None,
        decor: tuple[str, str, tuple[tuple[str, str], ...]] | None = None,
    ) -> Verdict:
        """What the guards tell the engine to do about this account now -- latching any breach
        that has just acted, so it holds even once the numbers recover."""
        if key is None:
            return NO_VERDICT
        report = self.report(key, snap, now, sizing_risk, decor)
        if not report.enabled:
            return NO_VERDICT
        rules = {r.id: r for r in self.doc.for_account(self._rules_key(key)).rules}
        held: list[str] = []
        held_by: list[str] = []
        news_by: list[str] = []
        stop_by = stop_warned_by = ""
        warnings: list[str] = []
        warned_by: list[str] = []
        stop_required = stop_warning = None
        news: set[str] = set()
        news_why: list[str] = []
        caps: dict[str, Cap] = {}
        lots_caps: list[ClassLotsCap] = []
        acts: list[NewsAct] = []
        closes: list[CloseAct] = []
        for status in report.rules:
            rule = REGISTRY.get(status.type)
            if rule is None or not status.enabled:
                continue
            if rule.needs_stop:
                if status.action == "warn":
                    stop_warning = status.title
                    stop_warned_by = status.rule_id
                else:
                    stop_required = status.title
                    stop_by = status.rule_id
                continue
            if rule.news:
                given = rules.get(status.rule_id)
                if given is None or self.calendar is None:
                    continue
                params, _ = given.resolved()
                calendar = self.calendar.current()
                due, before = acting(params, calendar, now)
                if due and status.action != "warn":
                    mode = str(params["before_news"])
                    acts.append(NewsAct(status.rule_id, status.title, mode, due, before))
                on_news, why = rule.held(params, calendar, now)  # type: ignore[attr-defined]
                if not on_news:
                    continue
                if status.action == "warn":
                    warnings.append(f"{status.title}: {why}")
                    warned_by.append(status.rule_id)
                else:
                    news |= on_news
                    news_why.append(why)
                    news_by.append(status.rule_id)
                continue
            if rule.lots_cap:
                given = rules.get(status.rule_id)
                if given is not None:
                    params, _ = given.resolved()
                    table = tuple(
                        (clas, float(params[clas])) for clas in CLASSES if float(params[clas]) > 0
                    )
                    if table:
                        lots_caps.append(
                            ClassLotsCap(
                                status.rule_id,
                                status.title,
                                str(params["on_breach"]),
                                str(params["scope"]),
                                table,
                            )
                        )
                continue
            if rule.cap is not None:
                if status.limit is not None:
                    cap = Cap(status.limit, status.action, status.title, status.rule_id)
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
            if status.action in _CLOSES:
                closes.append(
                    CloseAct(
                        status.rule_id,
                        status.title,
                        status.action,
                        _standing(status) or "past its limit",
                    )
                )
            if status.action in _HOLDS_BACK:
                held.append(line)
                held_by.append(status.rule_id)
            else:
                warnings.append(line)
                warned_by.append(status.rule_id)
        return Verdict(
            held_back=tuple(held),
            held_by=tuple(held_by),
            warnings=tuple(warnings),
            warned_by=tuple(warned_by),
            stop_required=stop_required,
            stop_warning=stop_warning,
            stop_warned_by=stop_warned_by,
            news=frozenset(news),
            news_why="; ".join(news_why),
            news_by=tuple(news_by),
            stop_by=stop_by,
            max_risk=caps.get("risk"),
            max_margin=caps.get("margin"),
            max_total_margin=caps.get("total_margin"),
            class_lots=tuple(lots_caps),
            news_acts=tuple(acts),
            close_acts=tuple(closes),
        )


def _standing(status: RuleStatus) -> str:
    """``at 84% of its limit``, with how a breach is being held; empty when neither is known."""
    fraction = status.fraction
    parts: list[str] = []
    if fraction is not None:
        parts.append(f"at {fraction:.0%} of its limit")
    if status.latched == "reset" and (fraction is None or fraction < (status.act_at or 1.0)):
        parts.append("held until the reset")
    elif status.latched == "hand":
        parts.append("held until cleared")
    return ", ".join(parts)


def _why(status: RuleStatus) -> str:
    """``Daily loss at 84% of its limit``, or that a breach is being held."""
    standing = _standing(status)
    return f"{status.title} {standing}" if standing else status.title


def _section(state: Mapping[str, object], name: str) -> dict[str, dict[str, Any]]:
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
        basis=rule.basis(params),
        per_trade=rule.per_trade(),
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
