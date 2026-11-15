"""The economic calendar the news guard reads, and which currencies a symbol trades.

**Where it comes from.** Forex Factory's weekly calendar feed, a JSON file of this week's
events with their time, currency and impact. Unofficial: its format can change and it can be
unreachable, so everything here can say "not known", and the rule decides what that means
(by default, hold new trades -- see :mod:`mt5copier.guards.rules.news`). The ``MetaTrader5``
package has no calendar of its own to read instead.

**When it is fetched.** Only while some account has a news rule -- nothing is fetched for a
copier that does not use one -- then hourly, on a thread of its own, never on the drain thread.
The last copy fetched is kept on disk, so a restart without the internet still knows the week.
It counts as known for a day after it was fetched. The feed limits how often it may be fetched;
hourly is well inside that.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .logging_setup import get_logger
from .paths import news_path

log = get_logger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_S = 3600.0
"""How often a calendar in use is fetched again."""
RETRY_S = 300.0
"""How long after a failed fetch the next is tried."""
FRESH_S = 24 * 3600.0
"""How long a fetched calendar counts as known."""
_TIMEOUT_S = 20.0

HIGH, MEDIUM = "High", "Medium"
# The feed resets connections whose User-Agent it does not recognise as a browser, and since
# 2026-11 the bare "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" prefix is no longer enough.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class NewsEvent:
    when: float
    """UTC seconds."""
    currency: str
    impact: str
    """``High``, ``Medium``, ``Low`` or ``Holiday``, as the feed says it."""
    title: str
    forecast: str = ""
    """The consensus number as the feed gives it (``0.3%``), or empty. Display only."""
    previous: str = ""
    """Last release's number, or empty. Display only."""


@dataclass(frozen=True, slots=True)
class Calendar:
    """What the calendar holds now. ``events`` is None when it is not known, and ``said``
    then says why."""

    events: tuple[NewsEvent, ...] | None
    said: str = ""


def parse_feed(raw: object) -> tuple[NewsEvent, ...]:
    """The feed's events. One that cannot be read is skipped; a feed with none readable at all
    is an error, since an empty week is far likelier to be a format change than a quiet one."""
    if not isinstance(raw, list):
        raise ValueError("the calendar is not a list of events")
    events: list[NewsEvent] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            when = datetime.fromisoformat(str(item["date"])).timestamp()
            events.append(
                NewsEvent(
                    when=when,
                    currency=str(item["country"]).upper(),
                    impact=str(item.get("impact", "")),
                    title=str(item.get("title", "")),
                    forecast=str(item.get("forecast") or ""),
                    previous=str(item.get("previous") or ""),
                )
            )
        except (KeyError, ValueError):
            continue
    if raw and not events:
        raise ValueError("no event in the calendar could be read")
    return tuple(sorted(events, key=lambda e: e.when))


def _fetch_feed() -> object:
    # The feed resets a connection that names an unknown client, so this reads as a browser.
    request = urllib.request.Request(FEED_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


class NewsCalendar:
    """The calendar, fetched on a thread of its own while it is wanted."""

    def __init__(
        self,
        path: Path | None = None,
        fetch: Callable[[], object] = _fetch_feed,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path if path is not None else news_path()
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._events: tuple[NewsEvent, ...] | None = None
        self._fetched_at = 0.0
        self._tried_at = 0.0
        self._error = ""
        self._wanted = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # --- reading ---------------------------------------------------------------------------

    def fetched_at(self) -> float:
        """When the calendar now held was fetched, UTC seconds; 0.0 when it never was."""
        with self._lock:
            return self._fetched_at

    def current(self) -> Calendar:
        """What is known now. Asking is what makes it wanted, so the thread starts fetching."""
        with self._lock:
            self._wanted = True
            now = self._clock()
            if self._events is not None and now - self._fetched_at <= FRESH_S:
                return Calendar(self._events)
            if self._events is None and not self._error and self._tried_at == 0.0:
                return Calendar(None, "the news calendar has not been fetched yet")
            why = self._error or "it is over a day old"
            return Calendar(None, f"the news calendar could not be read: {why}")

    # --- fetching --------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="news", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.due():
                self.refresh()
            self._stop.wait(5.0)

    def due(self) -> bool:
        with self._lock:
            if not self._wanted:
                return False
            now = self._clock()
            if now - self._tried_at < RETRY_S:
                return False
            return self._events is None or now - self._fetched_at >= REFRESH_S

    def refresh(self) -> bool:
        """Fetch the feed once. True if it was read; a failure keeps the last copy."""
        with self._lock:
            self._tried_at = self._clock()
        try:
            raw = self._fetch()
            events = parse_feed(raw)
        except Exception as exc:
            with self._lock:
                self._error = str(exc) or type(exc).__name__
            log.warning("news calendar fetch failed: %s", exc)
            return False
        now = self._clock()
        with self._lock:
            self._events, self._fetched_at, self._error = events, now, ""
        log.info("news calendar fetched: %d events this week", len(events))
        # Here, not at the top: the guards import this module, and their package imports rules.
        from .guards.model import write_json

        try:
            write_json(self._path, {"fetched_at": now, "events": raw})
        except OSError:
            log.exception("could not keep the news calendar at %s", self._path)
        return True

    def _load(self) -> None:
        try:
            kept = json.loads(self._path.read_text(encoding="utf-8"))
            events = parse_feed(kept["events"])
            fetched_at = float(kept["fetched_at"])
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning(
                "kept news calendar at %s unreadable (%s); fetching afresh", self._path, exc
            )
            return
        self._events, self._fetched_at = events, fetched_at


# --- which currencies a symbol trades ----------------------------------------------------------

CURRENCIES = frozenset(
    "USD EUR GBP JPY CHF CAD AUD NZD CNY CNH HKD SGD SEK NOK DKK PLN CZK HUF TRY ZAR MXN "
    "XAU XAG XPT XPD".split()
)
"""Codes a symbol's name is read for. Metals are priced in their own code (XAUUSD)."""

_NAMED: tuple[tuple[str, str], ...] = (
    # Indices, oil and crypto by the names brokers give them, to the currency whose news moves
    # them. Found anywhere in the name, so a broker's prefix or suffix does not hide them.
    ("US30", "USD"),
    ("US500", "USD"),
    ("US100", "USD"),
    ("US2000", "USD"),
    ("NAS", "USD"),
    ("USTEC", "USD"),
    ("SPX", "USD"),
    ("SP500", "USD"),
    ("DJ30", "USD"),
    ("DJI", "USD"),
    ("DOW", "USD"),
    ("USOIL", "USD"),
    ("WTI", "USD"),
    ("UKOIL", "USD"),
    ("BRENT", "USD"),
    ("XTI", "USD"),
    ("XBR", "USD"),
    ("BTC", "USD"),
    ("ETH", "USD"),
    ("GER", "EUR"),
    ("DE30", "EUR"),
    ("DE40", "EUR"),
    ("DAX", "EUR"),
    ("EU50", "EUR"),
    ("EUSTX", "EUR"),
    ("STOXX", "EUR"),
    ("FRA", "EUR"),
    ("F40", "EUR"),
    ("SPA35", "EUR"),
    ("ESP35", "EUR"),
    ("IT40", "EUR"),
    ("UK100", "GBP"),
    ("FTSE", "GBP"),
    ("JP225", "JPY"),
    ("JPN225", "JPY"),
    ("NIKKEI", "JPY"),
    ("AUS200", "AUD"),
    ("HK50", "HKD"),
    ("CHINA50", "CNY"),
    ("SWI20", "CHF"),
)


def symbol_currencies(symbol: str) -> frozenset[str] | None:
    """The currencies whose news moves this symbol, or None when its name cannot be read --
    which the news rule treats as moved by any news.

    A pair (``EURUSD``, ``EURUSD.r``, ``m.EURUSDp``) is its two codes; a named index, oil or
    coin is the currency in the table above.
    """
    name = re.sub(r"[^A-Z0-9]", "", symbol.upper())
    for start in range(len(name) - 5):
        first, second = name[start : start + 3], name[start + 3 : start + 6]
        if first in CURRENCIES and second in CURRENCIES and first != second:
            return frozenset({first, second})
    for part, currency in _NAMED:
        if part in name:
            return frozenset({currency})
    return None
