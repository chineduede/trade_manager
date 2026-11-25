"""Configuration schema and persistence.

Format is JSON, not YAML, and that is a deliberate reversal of the convention in ``zele-mt5``.
There, config was commented YAML that only humans wrote; here the app itself is the primary
editor, and round-tripping a ``model_dump()`` through ``yaml.dump`` destroys every comment in
the file. JSON has no comments to lose, so the machine can own the file outright without
quietly deleting the user's notes. Anything that needs explaining is explained in the README.

Config is keyed on ``terminal_id`` (the MT5 32-hex instance hash) rather than on the install
path. A terminal upgrade or reinstall can move the path while the data folder -- and therefore
the logged-in account -- stays put, so path-keyed config would silently orphan settings.

As of Stage 2a the filtering, inversion and no-stop-policy fields are read by the diff engine
in dry-run; the sizing fields (``risk_percent``, ``risk_cash``, ``max_lot``) are still unused,
because lots are computed against the slave's own terminal in Stage 2b. Adding them all up
front costs nothing and avoids a schema migration the first time copying is switched on.

Sizing is risk-based only: percent of the slave's own balance, or a fixed cash amount. There is
deliberately no lot-multiplier or fixed-lot mode, so a slave never inherits the master's
position size -- it sizes from its own balance and its own risk setting every time.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from . import shared_db
from .discovery import DiscoveredTerminal
from .json_store import quarantine, write_json
from .logging_setup import get_logger
from .paths import config_path

log = get_logger(__name__)

SCHEMA_VERSION = 2

_REMOVED_TERMINAL_FIELDS = (
    "default_sl_points",
    "custom_name",
    "breakeven_trigger_pips",
    "breakeven_buffer_pips",
)
"""Fields dropped from :class:`TerminalConfig`, stripped on load.

The schema is ``extra="forbid"``, so a key that no longer exists would fail validation, and
:func:`load` treats a validation failure as corruption and starts from defaults. Silently
discarding a retired key is much better than silently discarding the user's whole config.
"""


class Role(StrEnum):
    UNASSIGNED = "unassigned"
    MASTER = "master"
    SLAVE = "slave"


class AppMode(StrEnum):
    """What this profile is for: copying a real master, or trading directly from the app.

    Exclusive per profile, never both at once. In TRADING there is no master terminal at all;
    the master's place is taken by a *virtual* book the person trades from the app, and every
    enabled slave copies it exactly as it would copy a real master. Switching modes requires
    no open copies, in the same spirit as the orphaning rule: the other mode's master has
    different tickets, so copies carried across would read as closes.
    """

    COPY = "copy"
    TRADING = "trading"


VIRTUAL_MASTER_ID = "virtual-master"
"""The virtual master's stand-in terminal id, in a TRADING profile.

Deliberately not 32 hex characters, so it can never collide with a real MT5 instance hash --
and deliberately **never stored in** ``AppConfig.terminals``: it is not a terminal, has no
worker, claims nothing, and must not be offered roles, settings or logins. It exists only as
the id :meth:`AppConfig.master` answers with in trading mode, so the diff engine has a master
to key its snapshots on."""


class SizingMode(StrEnum):
    """How a slave's volume is derived. Unused until Stage 2.

    Both modes are **risk-based**: they convert an amount of money at risk into a lot size
    using the distance from entry to the master's stop loss. Neither scales the master's lot
    size, so the master's own position sizing is intentionally ignored -- each slave sizes from
    its own balance and its own risk setting.

    The critical consequence: **both modes need the master trade to have a stop loss.** With no
    stop there is no risk distance and no lot size can be derived, which is what
    :class:`NoStopPolicy` decides.
    """

    PERCENT_OF_BALANCE = "percent_of_balance"
    """Risk ``risk_percent`` of this slave's own balance on each trade."""

    FIXED_CASH_RISK = "fixed_cash_risk"
    """Risk exactly ``risk_cash`` per trade, in this slave's account currency."""

    # Both budgets are net of costs. Stage 2b derives lots as
    #     risk_amount / (loss_per_lot + commission_per_lot_round_turn + slippage_cost_per_lot)
    # where loss_per_lot comes from order_calc_profit over the master's entry-to-stop distance
    # and slippage_cost_per_lot from the same call over slippage_buffer_points. Every term is
    # per lot, so the equation stays linear and needs no iteration -- and the configured risk
    # becomes what the account can actually lose, not what it would lose in a frictionless fill.


class TerminalClosing(StrEnum):
    """What becomes of a terminal's process when the copier is done with it: the app quitting,
    or the terminal being unassigned. A restart or a reconnect never closes one."""

    NEVER = "never"
    """Leave every terminal running, even one the copier launched."""

    LAUNCHED = "launched"
    """Close a terminal only if the copier launched it; one that was already running is the
    person's own. The default, and what the copier always did before this was a setting."""

    ALL = "all"
    """Close every terminal the copier was connected to, including ones the person opened."""


class NoStopPolicy(StrEnum):
    """What to do when the master opens a position with no stop loss.

    Unavoidable, because risk-based sizing derives lots from the stop distance and there
    isn't one.
    """

    SKIP = "skip"
    """Do not copy the trade. The default, and off means off: a trade whose risk cannot be
    computed is better left uncopied."""

    LOT_MULTIPLE = "lot_multiple"
    """Copy at ``no_stop_lot_multiple`` times the master's own lot size.

    **The single exception to "a slave never inherits the master's position size."** It exists
    because with no stop there is no risk distance, so the master's own sizing is the only
    signal left; without it the trade simply cannot be copied at all.

    Two consequences to be clear-eyed about, neither of them a reason to refuse:

    * The money at risk is **not** the slave's configured risk, and is not knowable in advance
      -- an unstopped position's worst case is open-ended. ``risk_percent`` / ``risk_cash`` do
      not apply to these trades at all; ``max_lot`` still does.
    * The copy opens **unprotected**, exactly as the master's did. The UI marks it.
    """


class BreakevenMode(StrEnum):
    """What moves a slave's copies to breakeven, if anything. Slaves only."""

    OFF = "off"
    R = "r"
    """Once the copy is ``breakeven_trigger_r`` times its initial stop distance beyond its
    fill. A copy opened with no stop has no R, so it never triggers."""


class Management(StrEnum):
    """Who manages a copy once it is open. Set on the master; each slave can still keep its own
    trades (``TerminalConfig.master_manages``). See :meth:`AppConfig.manages`."""

    MASTER = "master"
    """Slaves follow the master's stop and target moves and partial closes."""
    SLAVES = "slaves"
    """Slaves copy only the master's opens and closes, and manage the rest themselves: their
    own breakeven and booking, none of the master's stop moves or partial closes."""


class TerminalConfig(BaseModel):
    """Per-terminal settings. Survives rescans; only the discovery-derived fields get refreshed."""

    model_config = ConfigDict(extra="forbid")

    terminal_id: str
    label: str = ""
    exe_path: Path | None = None
    role: Role = Role.UNASSIGNED
    enabled: bool = True
    """No longer offered anywhere in the UI -- pausing a slave is :attr:`paused` -- but kept in
    the schema, which forbids unknown keys, so a config file that has it still loads."""

    paused: bool = False
    """A paused slave is sent no *new* trades: the master's opens and new resting orders are
    declined for it, and like every decline that is final, so resuming does not copy what was
    opened in the meantime. Everything on copies it already holds -- closes, partials, stop and
    target moves, order changes and cancels -- carries on, and it stays connected. Meaningless
    on anything but a slave."""

    hidden: bool = False
    """Parked: the terminal is not spawned at all -- no connection, no copies, never the
    direct-trading feed, no card in the zones -- while its role and every setting are kept
    for the day it is unhidden. Unlike :attr:`paused` this is a full stop, and unlike
    unassigning it gives nothing up but the open copies it strands at the moment of hiding,
    which the person is asked about exactly as an unassign asks."""

    # --- Stage 2: sizing -------------------------------------------------------------------
    sizing_mode: SizingMode = SizingMode.PERCENT_OF_BALANCE

    risk_percent: float = Field(default=1.0, gt=0, le=100)
    """Used by PERCENT_OF_BALANCE. Percent of this slave's balance risked per trade."""

    risk_cash: float = Field(default=100.0, gt=0)
    """Used by FIXED_CASH_RISK. Amount risked per trade, in this slave's own account
    currency -- deliberately not converted from a base currency, since a slave's currency is
    what its balance and its broker's tick values are already denominated in."""

    max_lot: float | None = Field(default=None, gt=0)
    """Hard ceiling on computed volume, regardless of mode. Prop accounts often impose one."""

    commission_per_lot_round_turn: float = Field(default=0.0, ge=0)
    """Commission this broker charges for one lot in *and* out, in the account currency.

    Named for the round turn on purpose: brokers quote commission per lot per side (a "$3.50
    commission" is usually $7.00 to open and close one lot), and halving or doubling it by
    accident is a silent sizing error. Enter the full there-and-back cost.

    It belongs in sizing because it is money lost on a trade that hits its stop, so it comes
    out of the risk budget before lots are derived -- otherwise every trade risks the
    configured amount *plus* commission, and the smaller the stop, the worse the overshoot.

    Breakeven mostly does without it: a copy's stop goes past entry by the commission its own
    opening deal was charged, doubled. This is the fallback for a copy whose deal showed none."""

    slippage_buffer_points: int = Field(default=0, ge=0)
    """Points of adverse fill to assume when sizing, on entry and on the stop together.

    Sizing from the master's exact entry-to-stop distance assumes both fills are perfect. They
    are not, and both errors run the same way: a worse entry and a worse stop fill each widen
    the real loss. Budgeting for it here makes the configured risk a ceiling rather than a
    best case. Zero reproduces the naive calculation."""

    max_deviation_points: int = Field(default=20, ge=0)
    """Maximum price deviation to accept on an order, i.e. MT5's ``deviation`` request field.

    Execution only -- unlike the two fields above, this never affects lot size. Kept per slave
    because a sensible value depends on the instrument and the broker's spread."""

    no_stop_policy: NoStopPolicy = NoStopPolicy.SKIP
    """Defaults to SKIP, and that default is deliberate: copying an unstopped trade is opt-in
    per slave, because it is the one case where the risk cannot be bounded in advance."""

    no_stop_lot_multiple: float = Field(default=1.0, gt=0)
    """Multiplier on the *master's* lot size, used only when no_stop_policy is LOT_MULTIPLE.

    1.0 mirrors the master's size exactly; 0.5 takes half. Unused -- and irrelevant -- for any
    trade that has a stop, which is sized from the slave's own risk instead."""

    allow_live_trading: bool = False
    """Permit copying into an account that is not a demo.

    Off, and it has to be turned on per terminal, deliberately. Most of the terminals on this
    machine are prop or live; a role is one keypress away from landing on the wrong one, and
    the worker refuses to trade a non-demo account without this. An account whose trade mode
    cannot be read counts as not-demo -- "we could not tell" is not a reason to trade someone's
    funded account."""

    # --- Stage 2: copy behaviour -----------------------------------------------------------
    invert: bool = False
    management: Management = Management.MASTER
    """The master's: who manages copies once they are open. Ignored on a slave."""
    master_manages: bool = True
    """A slave's: whether it lets the master manage its copies, when the master is set to.
    Off, it manages its own whatever the master says. Replaced ``copy_sltp`` and
    ``copy_partials``, taking ``copy_sltp``'s value."""
    copy_pending: bool = False
    """Mirror pending orders (limit/stop) as well as open positions. Stage 4; unused so far."""

    pending_fallback: bool = True
    """Rescue a pending copy whose price the broker refuses, instead of dropping the trade.

    Two retries, in order: once as the other resting type at the master's price (a limit as a
    stop, a stop as a limit -- the two differ only in which side of the quote they may rest
    on, and a feed that has already crossed the master's price is the routine refusal), then
    once at market. A copy that went to market is an open position from the start; it is
    closed if the master cancels the order, and managed normally once the master's order
    fills. On by default, because the alternative is the trade silently not copying; turn it
    off on a slave that must only ever rest exactly what the master rests."""

    tp_net_rr: bool = True
    """Direct trading only: land this slave's target past the ticket's by (rr + 1) of its own
    commissions, so a win pays the ticket's risk:reward after costs. The pad is computed once,
    when the trade is first written, and survives every later stop move. Off, the ticket's
    target is sent exactly as written and commission comes out of the win. Ignored when
    copying a real master -- that trader's level is never touched either way."""

    # --- breakeven ---------------------------------------------------------------------------
    breakeven_mode: BreakevenMode = BreakevenMode.OFF
    breakeven_trigger_r: float = Field(default=1.0, gt=0)
    """How far in profit, in multiples of the copy's opening stop distance, before its stop
    moves: 0.3 is 0.3R. Where it moves to is not a setting of its own: entry, plus enough to
    pay the copy's commission (its fill's, else ``commission_per_lot_round_turn``), plus
    ``slippage_buffer_points``. See
    :mod:`mt5copier.breakeven`."""
    breakeven_book_percent: float = Field(default=0.0, ge=0, lt=100)
    """The percent of a copy's volume to close when its breakeven fires, booking that much of
    the profit; 0 books nothing. Once per copy, whether the breakeven came from the R trigger
    or a price set by hand. A slice, or a remainder, under the symbol's minimum lot is not
    booked at all: the position stays whole and only its stop moves."""

    symbol_prefix: str = ""
    symbol_suffix: str = ""
    """This broker's decoration on symbol names, whichever side of the copy it is on.

    On a slave it is put *on* the master's name to find this broker's. On the master it is
    taken *off* every name before anything else sees it (see :func:`mt5copier.symbols.undecorate`),
    so the slaves' renames, filters and decorations are all written against the plain name. One
    meaning for both roles is why a terminal keeps it when its role changes: it describes the
    broker, not the role."""
    symbol_map: dict[str, str] = Field(default_factory=dict)
    """Exact per-symbol overrides: the master's name to *this slave's* name.

    For the exceptions a single decoration cannot express. Brokers decorate by asset class --
    BlackBull suffixes 1978 symbols with ``p`` but leaves ``US30`` and ``NAS100`` bare -- so
    the prefix/suffix above covers the bulk and this covers the rest. Only symbols that are
    genuinely exceptional need an entry; anything the decoration already resolves should be
    left out rather than restated.

    Keys are normalised to upper case on load, so they match the master's name however it was
    typed. Values keep the broker's own casing, because that is what ``symbol_info`` wants --
    though resolution folds case against the real symbol list, so a mistyped value still
    resolves if the instrument exists.

    An entry naming a symbol the slave does not list is a **refusal**, not a fallback: the
    entry is a human assertion that these two names are the same instrument, and if it is
    wrong then falling through would place the trade on something the user never named while
    the log reported a match. See :mod:`mt5copier.symbols`.
    """
    symbol_whitelist: list[str] = Field(default_factory=list)
    symbol_blacklist: list[str] = Field(default_factory=list)

    @field_validator("symbol_map", mode="before")
    @classmethod
    def _normalise_symbol_map(cls, value: object) -> object:
        """Upper-case the keys once, here, rather than folding at every lookup.

        Three places resolve a symbol -- the executor, the log's approximation in
        :func:`mt5copier.reconcile.map_symbol`, and the UI's mapping tester -- and a fold
        repeated in three places is a fold that eventually disagrees with itself.

        Two keys that differ only in case but name *different* targets are refused rather
        than resolved by dict order. Picking one would be a guess about which instrument the
        user meant, which is the single thing symbol resolution must never do. It is only
        reachable by hand-editing the file into a contradiction; the editor rejects it at
        entry.
        """
        if not isinstance(value, dict):
            return value
        out: dict[str, str] = {}
        for raw_key, raw_target in value.items():
            key = str(raw_key).strip().upper()
            target = str(raw_target).strip()
            if key in out and out[key] != target:
                raise ValueError(
                    f"symbol_map has two entries for {key!r} that disagree: "
                    f"{out[key]!r} and {target!r}"
                )
            out[key] = target
        return out

    # --- Last known passive discovery data, for display before connecting ------------------
    last_login: int | None = None
    last_server: str | None = None

    # --- cTrader -----------------------------------------------------------------------------
    ctrader_account_id: int | None = None
    """Set only on a cTrader account: its ``ctidTraderAccountId``. Such a terminal has no
    executable -- it is reached over the Open API -- and can only ever be a slave."""
    ctrader_live: bool = False
    """Whether that account is live, which decides the host it is reached on. The demo-only
    guard reads the account's own report, not this."""

    @property
    def is_ctrader(self) -> bool:
        return self.ctrader_account_id is not None

    @property
    def usable(self) -> bool:
        """Whether the copier can connect to it: an MT5 terminal's executable is here, or it is
        a cTrader account."""
        return self.is_ctrader or self.exe_path is not None

    # No cross-field validator: every sizing field is independently constrained by its own
    # Field(), and LOT_MULTIPLE needs nothing but a positive multiplier, which gt=0 gives.


_VIRTUAL_MASTER: TerminalConfig | None = None


def virtual_master() -> TerminalConfig:
    """The virtual master's stand-in config, shared and **read-only by convention**.

    A single cached instance rather than a fresh one per call, because :meth:`AppConfig.master`
    is read on every reconcile tick and per slave for :meth:`AppConfig.manages`. Everything the
    engine reads off a master config is deliberately at its default here: no symbol decoration
    (the book is kept in clean names), breakeven off (a direct trade's stop is the person's
    own), management MASTER (the app propagates the person's stop moves and partials to every
    copy -- that is the whole point of direct mode; a slave's ``master_manages`` opt-out is
    still honoured as usual).
    """
    global _VIRTUAL_MASTER
    if _VIRTUAL_MASTER is None:
        _VIRTUAL_MASTER = TerminalConfig(
            terminal_id=VIRTUAL_MASTER_ID,
            label="Direct trading",
            role=Role.MASTER,
        )
    return _VIRTUAL_MASTER


def account_key(login: int | None, server: str | None) -> str | None:
    """Canonical key for naming an *account*, not a terminal -- ``None`` when no account is
    known yet (nothing to name).

    A terminal installation can log into more than one account over its life (a demo today, a
    funded account tomorrow), so a name kept on :class:`TerminalConfig` would follow whichever
    account happened to be logged in when it was set, not the account itself -- exactly backward
    from what a rename is for. Keyed by account instead, the same name reappears wherever that
    account is logged in, on any terminal, and a different account logged into the same terminal
    shows its own name (or none) rather than inheriting one that was never about it.

    ``server`` is part of the key, not an afterthought: an MT5 login number is only unique
    *within* one broker's own servers, and two different brokers can and do hand out the same
    account number -- a key on the login alone would silently merge two unrelated accounts'
    names the day that collision happened to occur.
    """
    if login is None:
        return None
    return f"{login}@{server or ''}"


def record_login(tcfg: TerminalConfig, login: int | None, server: str | None) -> TerminalConfig:
    """The terminal's config as it should read after a login landed it on this account.

    ``last_login``/``last_server`` follow, and the live-trading override is withdrawn when a
    *known* account was left for a different one: the override was typed and confirmed while
    the terminal was on that account, and the one it moved to never got that confirmation.
    Landing on an account where none was known is learning, not a switch, so the override is
    kept -- a portable install whose account discovery cannot read would otherwise lose a
    deliberate override on its first connection.
    """
    update: dict[str, object] = {"last_login": login, "last_server": server}
    was = account_key(tcfg.last_login, tcfg.last_server)
    if tcfg.allow_live_trading and was is not None and was != account_key(login, server):
        update["allow_live_trading"] = False
    return tcfg.model_copy(update=update)


POLICY_FIELDS: tuple[str, ...] = (
    "sizing_mode",
    "risk_percent",
    "risk_cash",
    "max_lot",
    "commission_per_lot_round_turn",
    "slippage_buffer_points",
    "no_stop_policy",
    "no_stop_lot_multiple",
    "invert",
    "management",
    "master_manages",
    "copy_pending",
    "pending_fallback",
    "tp_net_rr",
    "breakeven_mode",
    "breakeven_trigger_r",
    "breakeven_book_percent",
    "symbol_whitelist",
    "symbol_blacklist",
)
"""The trader-policy fields of :class:`TerminalConfig`: what a person tunes for a particular
account's money, as opposed to facts about the install. A 1% risk sized against a 100k
account has no business following the terminal onto a 10k account, so these are remembered
per account (:func:`remember_policy`) and brought back when a switch returns to it
(:func:`restore_policy`).

Deliberately absent: the broker facts (symbol decoration and map, ``max_deviation_points``),
which describe the install's broker and must survive a switch exactly as they do today; the
plumbing (role, paused, label, paths); and ``allow_live_trading``, whose typed consent is
withdrawn on a switch (:func:`record_login`) and never restored by machinery -- the scary
direction keeps its friction."""


def snapshot_policy(tcfg: TerminalConfig) -> dict[str, object]:
    """The terminal's policy block as a plain JSON-ready dict, for remembering per account."""
    return tcfg.model_dump(mode="json", include=set(POLICY_FIELDS))


def remember_policy(tcfg: TerminalConfig) -> None:
    """Save the terminal's policy under the account it is on now, if that account is known.

    The memory lives in ``shared.db`` (the ``account_policies`` table), keyed by the
    account alone -- so it is one memory for the whole machine, every profile. A profile
    owns its terminals; what an account runs with belongs to the account, wherever it is
    next logged in.

    Called in two situations. Where settings are about to stop being that account's active
    settings -- the app's own account switch, and a discovery sweep that notices a login
    made in MT5 itself -- the call must come *before* the change, while the settings and
    the account still correspond: afterwards the terminal holds the new settings, and
    remembering then would file them under an account they were never tuned for. And on
    every deliberate settings save, where the saved config *is* the account's newest
    settings -- banking it at once is what lets every other window's poll show the account
    tuned the same way (``DesktopApi._refresh_policies``). Saving is lossless and silent,
    so it never needs a question; the newest save for an account simply wins, because the
    terminal's current settings are by construction the last ones in force on it.
    """
    key = account_key(tcfg.last_login, tcfg.last_server)
    if key is None:
        return
    db = shared_db.get()
    with db.change(shared_db.POLICIES):
        db.put("account_policies", key, snapshot_policy(tcfg))


def restore_policy(tcfg: TerminalConfig) -> tuple[TerminalConfig, bool]:
    """The terminal's config with the policy last used on its account, if one is remembered.

    The counterpart of :func:`remember_policy`, reading the same shared table, applied
    where a terminal starts holding an account: the app's own account switch, and the
    discovery paths in :func:`merge_discovery` -- a brand-new profile's first sweep finds
    its terminals' accounts already tuned. The desktop's poll also folds a newer bank onto
    terminals already holding the account (``DesktopApi._refresh_policies``), so an account
    reads as one live set of settings machine-wide, never a per-profile copy.

    Keys the snapshot carries that the schema no longer has are ignored; a snapshot the
    current schema rejects outright restores nothing rather than half of itself.
    """
    key = account_key(tcfg.last_login, tcfg.last_server)
    saved = shared_db.get().fetch("account_policies", key) if key is not None else None
    if not isinstance(saved, dict) or not saved:
        return tcfg, False
    merged = tcfg.model_dump(mode="json")
    merged.update({k: v for k, v in saved.items() if k in POLICY_FIELDS})
    try:
        return TerminalConfig.model_validate(merged), True
    except ValidationError:
        log.warning("the settings remembered for %s no longer load; keeping the current ones", key)
        return tcfg, False


class AppConfig(BaseModel):
    """Root config document."""

    model_config = ConfigDict(extra="forbid")

    version: int = SCHEMA_VERSION

    mode: AppMode = AppMode.COPY
    """Whether this profile copies a real master terminal or trades directly from the app.
    See :class:`AppMode`. Changed only through :meth:`mt5copier.engine.Engine.set_mode`, which
    refuses while any copies are open."""

    feed_terminal_id: str | None = None
    """TRADING only: the terminal whose quotes feed the virtual master, pinned by hand. None
    picks any connected MT5 slave automatically, failing over when it drops. A pin that is not
    connected right now falls back to auto rather than leaving the book blind."""

    poll_interval_ms: int = Field(default=250, ge=50, le=10_000)
    """How often each worker refreshes its account snapshot."""

    heartbeat_timeout_s: float = Field(default=15.0, gt=0)
    """A worker whose newest snapshot is older than this is shown as stale, not merely
    disconnected -- it distinguishes "terminal lost its broker" from "our process wedged"."""

    rescan_interval_s: float = Field(default=120.0, ge=10, le=3600)
    """How often terminals are re-discovered from disk, so an *unassigned* terminal's card
    follows a login made in MT5 itself without the rescan button. Connected terminals do not
    need it -- their workers report the account live -- and the sweep is passive directory
    reading, so the floor only guards against hammering a slow disk."""

    reconnect_backoff_s: list[float] = Field(default_factory=lambda: [1.0, 2.0, 5.0, 10.0, 30.0])
    """Backoff schedule for reconnect attempts; the last value repeats indefinitely."""

    copying_enabled: bool = False
    """Master switch for Stage 2: while False the diff engine does not even run. Panic sets it
    back to False without ever closing a position."""

    join_grace_seconds: float = Field(default=5.0, ge=0, le=5)
    """Longest lapse in observation that still counts as continuous watching.

    A position may only be copied if we watched it appear. If observation of the master and a
    given slave lapses for longer than this -- a link blip, a terminal restart, the app being
    closed -- everything the master already holds becomes permanently uncopyable for that
    slave, because a late join fills far from the master's entry and silently breaks the risk
    sizing.

    A few seconds is not a late join, which is what this window is for: a trade opened four
    seconds ago fills within a whisker of the master's price. Capped at 5 seconds on purpose --
    a larger window would quietly reintroduce the problem it exists to avoid. Zero disables the
    grace entirely, so any disconnection at all excludes what was open."""

    close_terminals: TerminalClosing = TerminalClosing.LAUNCHED
    """Whether a terminal's process is closed when the copier quits or it is unassigned."""

    dry_run: bool = True
    """Detect and report intended actions, but send nothing to any broker.

    Defaults to true and stays true for the whole of Stage 2a, where no execution code exists
    at all -- the engine refuses to act on a false value it cannot honour rather than appearing
    to go live. Turning it off has to be a deliberate act, taken after the dry-run log has been
    read and believed."""

    history_days: int = Field(default=30, ge=1, le=365)
    """How many days of each trade's history to keep: what every slave did with every master
    trade. Older steps are deleted from ``history.db``."""

    terminals: dict[str, TerminalConfig] = Field(default_factory=dict)

    account_names: dict[str, str] = Field(default_factory=dict)
    """Names the person set, keyed by :func:`account_key` -- the *account*, not the terminal it
    happens to be logged into right now. See :func:`account_key`'s own docstring for why."""

    @model_validator(mode="after")
    def _validate_roles(self) -> Self:
        masters = [t.terminal_id for t in self.terminals.values() if t.role is Role.MASTER]
        if len(masters) > 1:
            raise ValueError(f"only one terminal may be master, got {len(masters)}: {masters}")
        if any(t.is_ctrader for t in self.terminals.values() if t.role is Role.MASTER):
            raise ValueError("a cTrader account can only be a slave")
        for tid, tcfg in self.terminals.items():
            if tcfg.terminal_id != tid:
                raise ValueError(f"terminals key {tid!r} != terminal_id {tcfg.terminal_id!r}")
        return self

    # --- convenience ------------------------------------------------------------------------

    @property
    def master(self) -> TerminalConfig | None:
        """The master the slaves copy: a designated terminal, or in trading mode the virtual
        one. Returning the virtual config here, rather than special-casing every caller, is
        what lets the reconciler, ``manages()`` and the orphaned-slave repair all treat direct
        mode as an ordinary master that happens not to be a terminal."""
        if self.mode is AppMode.TRADING:
            return virtual_master()
        return next((t for t in self.terminals.values() if t.role is Role.MASTER), None)

    @property
    def slaves(self) -> list[TerminalConfig]:
        return [t for t in self.terminals.values() if t.role is Role.SLAVE]

    def manages(self, slave: TerminalConfig) -> bool:
        """Whether a trade this slave opens now is managed by the master: both gates open, the
        master set to manage and the slave letting it. Fixed on each copy when it opens, so a
        change reaches only trades opened after it."""
        master = self.master
        return (
            master is not None and master.management is Management.MASTER and (slave.master_manages)
        )

    def assigned(self) -> list[TerminalConfig]:
        """Terminals we should hold a connection to: any with a role, enabled, not parked."""
        return [
            t
            for t in self.terminals.values()
            if t.role is not Role.UNASSIGNED and t.enabled and not t.hidden
        ]

    def display_name(self, terminal_id: str) -> str:
        """The name to show this terminal by right now: the name set for whichever account it's
        last known to be logged into (see :func:`account_key`), else discovery's own ``label``,
        else the id's first eight hex characters -- the same last-resort the log and every
        notification already fall back to. One place for a fallback chain several call sites
        used to repeat by hand (``cfg.label or terminal_id[:8]``), so a rename shows up
        everywhere a terminal's name already does without hunting down every one of those call
        sites, only the one place this actually reads the name from.
        """
        if terminal_id == VIRTUAL_MASTER_ID:
            # Not in ``terminals`` and never will be; named here so no caller that happens
            # to hold the virtual id -- a log line, the day book -- can crash on it.
            return virtual_master().label
        tcfg = self.terminals[terminal_id]
        key = account_key(tcfg.last_login, tcfg.last_server)
        if key is not None:
            name = self.account_names.get(key)
            if name:
                return name
        return tcfg.label or terminal_id[:8]

    def set_role(self, terminal_id: str, role: Role) -> None:
        """Assign a role, demoting any existing master so the one-master rule always holds.

        A pause belongs to one spell as a slave and does not survive a change of role: a slave
        unassigned while paused and later made a slave again starts out copying.
        """
        if terminal_id not in self.terminals:
            raise KeyError(terminal_id)
        if role is Role.MASTER and self.mode is AppMode.TRADING:
            raise ValueError(
                "this profile trades directly, so it has no master terminal; "
                "switch it to copy mode first"
            )
        if role is Role.MASTER:
            for tcfg in self.terminals.values():
                if tcfg.role is Role.MASTER and tcfg.terminal_id != terminal_id:
                    tcfg.role = Role.UNASSIGNED
                    tcfg.paused = False
        target = self.terminals[terminal_id]
        if target.role is not role:
            target.paused = False
        target.role = role

    def replace_terminal(self, new: TerminalConfig) -> None:
        """Swap in an edited copy of one terminal's settings, as a single assignment.

        The engine's drain thread reads a slave's settings on every tick while the UI thread is
        the one writing them, and neither holds a lock the other respects. Setting fields one at
        a time would let an order be sized from half of an edit -- percent mode beside a cash
        amount that was meant for the other mode, say. One dict assignment is atomic, so a
        reader sees the settings from before the edit or after it and never a mixture.

        A change of role is refused rather than carried across. :meth:`set_role` owns role
        changes because it is the only place the one-master rule is kept, and an edit that
        slipped a second master in through here would bypass it.
        """
        old = self.terminals.get(new.terminal_id)
        if old is None:
            raise KeyError(new.terminal_id)
        if new.role is not old.role:
            raise ValueError("a settings edit cannot change a terminal's role; use set_role")
        self.terminals[new.terminal_id] = new


def _demote_master_in_trading(cfg: AppConfig) -> list[str]:
    """A trading profile has no master terminal, but a config hand-edited into holding both
    the mode and a MASTER role can exist. Repaired on load like the orphaned slaves below,
    and for the same reason: a validator would reset the whole config to defaults over a role
    field. The virtual master takes the demoted terminal's place, so its slaves keep theirs.
    """
    if cfg.mode is not AppMode.TRADING:
        return []
    demoted = [t.terminal_id for t in cfg.terminals.values() if t.role is Role.MASTER]
    for tid in demoted:
        cfg.terminals[tid].role = Role.UNASSIGNED
        cfg.terminals[tid].paused = False
    return demoted


def _demote_orphaned_slaves(cfg: AppConfig) -> list[str]:
    """A slave with no master makes no sense -- copying has nothing to watch -- but a config
    written before :func:`mt5copier.role_actions.unassign` learned to sweep slaves along with
    the master it's unassigning, or one hand-edited into that shape, can still hold one.

    Fixed here, on load, the same way :func:`_migrate` fixes schema drift -- deliberately **not**
    a model validator next to :meth:`AppConfig._validate_roles`'s one-master rule. A validator
    would make :meth:`AppConfig.model_validate` raise on exactly the file this exists to repair,
    and :func:`load` treats a validation failure as corruption and resets to defaults: destroying
    every one of the user's settings to fix a role field would be a far worse outcome than
    quietly correcting it and moving on.
    """
    if cfg.master is not None:
        return []
    swept = [t.terminal_id for t in cfg.terminals.values() if t.role is Role.SLAVE]
    for tid in swept:
        cfg.terminals[tid].role = Role.UNASSIGNED
    return swept


def _migrate(raw: dict[str, object]) -> dict[str, object]:
    """Bring an older config document up to the current schema, in place.

    Kept deliberately small and forgiving: this runs before validation, on a document that may
    have been written by any previous version, so it must never raise on a shape it does not
    recognise. Anything genuinely malformed is caught by validation afterwards.
    """
    terminals = raw.get("terminals")
    if not isinstance(terminals, dict):
        return raw
    for entry in terminals.values():
        if not isinstance(entry, dict):
            continue
        # ``custom_name`` was a per-terminal name, briefly saved by a development build before
        # names moved to the account (see :func:`account_key`). Keep any that were set by filing
        # them under the account the terminal was last logged into, then drop the field.
        name = entry.get("custom_name")
        key = account_key(entry.get("last_login"), entry.get("last_server"))
        if isinstance(name, str) and name.strip() and key is not None:
            names = raw.setdefault("account_names", {})
            if isinstance(names, dict):
                names.setdefault(key, name.strip())
        for field_name in _REMOVED_TERMINAL_FIELDS:
            if entry.pop(field_name, None) is not None:
                log.info("dropped retired config field %r", field_name)
        # copy_sltp and copy_partials became one switch (2026-10-10), the master managing a
        # slave's copies or not. It takes copy_sltp's value: following the master's stops was
        # what most of that management was.
        if "copy_sltp" in entry or "copy_partials" in entry:
            follow = entry.pop("copy_sltp", True)
            entry.pop("copy_partials", None)
            entry.setdefault("master_manages", bool(follow))
        # Breakeven briefly had a pips trigger (2026-10-03), replaced by R or a hand-set price
        # per position the same day. Turned off rather than guessed at as some R.
        if entry.get("breakeven_mode") == "pips":
            entry["breakeven_mode"] = "off"
            log.warning(
                "breakeven by pips is no longer offered; %s now has breakeven off",
                entry.get("label") or "a slave",
            )
    raw["version"] = SCHEMA_VERSION
    return raw


def merge_discovery(cfg: AppConfig, discovered: list[DiscoveredTerminal]) -> AppConfig:
    """Fold a discovery sweep into existing config without clobbering user choices.

    Refreshes only the facts discovery owns (label, exe path, last login/server). Roles,
    sizing and filters are the user's and are left alone. Terminals that have disappeared are
    *kept* rather than deleted -- a terminal mid-upgrade should not silently lose its risk
    settings -- but they will report as unusable in the UI.
    """
    for d in discovered:
        existing = cfg.terminals.get(d.terminal_id)
        if existing is None:
            fresh = TerminalConfig(
                terminal_id=d.terminal_id,
                label=d.label,
                exe_path=d.exe_path,
                last_login=d.login,
                last_server=d.server,
            )
            # A terminal this profile has never configured carries no choices to protect,
            # so the account's remembered settings go straight on -- this is how a brand
            # new profile shows every known account already tuned.
            fresh, restored = restore_policy(fresh)
            cfg.terminals[d.terminal_id] = fresh
            log.info(
                "new terminal discovered: %s (%s)%s",
                d.label,
                d.terminal_id[:8],
                "; its account's remembered settings applied" if restored else "",
            )
        else:
            existing.label = d.label
            if d.exe_path is not None:
                if existing.exe_path != d.exe_path:
                    log.info(
                        "terminal %s moved: %s -> %s",
                        d.terminal_id[:8],
                        existing.exe_path,
                        d.exe_path,
                    )
                existing.exe_path = d.exe_path
            # A switch made in MT5 itself still swaps the policy settings with the
            # account: the one it leaves banks what was tuned on it, the one it lands on
            # gets back whatever is remembered for it. An unchanged login touches
            # nothing, so the person's edits in this profile are never overwritten by a
            # mere sweep.
            now = account_key(
                d.login if d.login is not None else existing.last_login,
                d.server if d.server is not None else existing.last_server,
            )
            was = account_key(existing.last_login, existing.last_server)
            if was is not None and now != was:
                remember_policy(existing)
            if d.login is not None:
                existing.last_login = d.login
            if d.server is not None:
                existing.last_server = d.server
            if now is not None and now != was:
                swapped, restored = restore_policy(existing)
                if restored:
                    cfg.terminals[d.terminal_id] = swapped
                    log.info(
                        "terminal %s switched to %s in MT5; the settings last used on "
                        "that account are back",
                        d.terminal_id[:8],
                        now,
                    )
    return cfg


CTRADER_PREFIX = "CTRADER:"


def ctrader_server_name(broker: str, is_live: bool) -> str:
    """What stands in for an MT5 server name on a cTrader account, so :func:`account_key` and
    the account names work for it as they do for MT5."""
    return f"{broker or 'cTrader'}-cTrader-{'Live' if is_live else 'Demo'}"


def ctrader_terminal_id(account_id: int) -> str:
    return f"{CTRADER_PREFIX}{account_id}"


def merge_ctrader_accounts(
    cfg: AppConfig, accounts: list[tuple[int, bool, int | None, str]]
) -> list[str]:
    """Fold the accounts a cTrader login allows, as ``(account_id, is_live, login, broker)``,
    into the terminal list. Returns the ids of those that are new.

    As with discovery, an account that is no longer allowed is kept rather than deleted, so its
    settings survive a login that left it out by mistake; its worker will say it is refused."""
    added: list[str] = []
    for account_id, is_live, login, broker in accounts:
        tid = ctrader_terminal_id(account_id)
        label = (
            f"{broker or 'cTrader'} cTrader {'Live' if is_live else 'Demo'} {login or account_id}"
        )
        server = ctrader_server_name(broker, is_live)
        existing = cfg.terminals.get(tid)
        if existing is None:
            cfg.terminals[tid] = TerminalConfig(
                terminal_id=tid,
                label=label,
                ctrader_account_id=account_id,
                ctrader_live=is_live,
                last_login=login,
                last_server=server,
            )
            added.append(tid)
            log.info("cTrader account added: %s", label)
        else:
            existing.label = label
            existing.ctrader_live = is_live
            existing.last_server = server
            if login is not None:
                existing.last_login = login
    return added


def load(path: Path | None = None) -> AppConfig:
    """Load config, returning defaults if the file is absent.

    A corrupt file is renamed aside rather than deleted, and defaults are returned, so a bad
    write can never leave the app unable to start.
    """
    from . import shared_data  # late: shared_data type-hints AppConfig from this module

    target = path or config_path()
    if not target.exists():
        log.info("no config at %s, starting from defaults", target)
        cfg = AppConfig()
        shared_data.apply(cfg)
        return cfg
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        cfg = AppConfig.model_validate(_migrate(raw))
        demoted = _demote_master_in_trading(cfg)
        if demoted:
            log.warning(
                "this profile trades directly, so its master terminal was unassigned: %s",
                demoted,
            )
        swept = _demote_orphaned_slaves(cfg)
        if swept:
            log.warning("slave(s) with no master found on load; unassigned: %s", swept)
        if demoted or swept:
            try:
                save(cfg, target)
            except OSError:
                log.exception("could not persist the role cleanup; will retry on next save")
        shared_data.apply(cfg)
        return cfg
    except Exception as exc:
        quarantine(target, str(exc), "config")
        cfg = AppConfig()
        shared_data.apply(cfg)
        return cfg


def save(cfg: AppConfig, path: Path | None = None) -> Path:
    """Write config atomically (:func:`mt5copier.json_store.write_json`)."""
    target = path or config_path()
    write_json(target, cfg.model_dump(mode="json", exclude_none=False))
    log.debug("config saved to %s", target)
    return target
