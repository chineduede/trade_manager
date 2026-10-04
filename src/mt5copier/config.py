"""Configuration schema and persistence.

Format is JSON, not YAML, and that is a deliberate reversal of the convention in ``zele-mt5``.
There, config was commented YAML that only humans wrote; here the Textual UI is the primary
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
import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .discovery import DiscoveredTerminal
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


BREAKEVEN_FIELDS = frozenset({"breakeven_mode", "breakeven_trigger_r"})
"""The settings an edit may change without giving up the slave's open copies. They change how
a copy's stop is managed from here on, never its direction or size."""


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
    configured amount *plus* commission, and the smaller the stop, the worse the overshoot."""

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
    copy_sltp: bool = True
    copy_partials: bool = True
    copy_pending: bool = False
    """Mirror pending orders (limit/stop) as well as open positions. Stage 4; unused so far."""

    # --- breakeven ---------------------------------------------------------------------------
    breakeven_mode: BreakevenMode = BreakevenMode.OFF
    breakeven_trigger_r: float = Field(default=1.0, gt=0)
    """How far in profit, in multiples of the copy's opening stop distance, before its stop
    moves: 0.3 is 0.3R. Where it moves to is not a setting of its own: entry, plus enough to
    pay ``commission_per_lot_round_turn``, plus ``slippage_buffer_points``. See
    :mod:`mt5copier.breakeven`."""

    symbol_prefix: str = ""
    symbol_suffix: str = ""
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

    # No cross-field validator: every sizing field is independently constrained by its own
    # Field(), and LOT_MULTIPLE needs nothing but a positive multiplier, which gt=0 gives.

    def risk_label(self) -> str:
        """Short description of this slave's risk setting, for the UI."""
        if self.role is not Role.SLAVE:
            return "-"
        if self.sizing_mode is SizingMode.FIXED_CASH_RISK:
            return f"{self.risk_cash:g} cash"
        return f"{self.risk_percent:g}%"


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


class AppConfig(BaseModel):
    """Root config document."""

    model_config = ConfigDict(extra="forbid")

    version: int = SCHEMA_VERSION

    poll_interval_ms: int = Field(default=250, ge=50, le=10_000)
    """How often each worker refreshes its account snapshot."""

    heartbeat_timeout_s: float = Field(default=15.0, gt=0)
    """A worker whose newest snapshot is older than this is shown as stale, not merely
    disconnected -- it distinguishes "terminal lost its broker" from "our process wedged"."""

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
        for tid, tcfg in self.terminals.items():
            if tcfg.terminal_id != tid:
                raise ValueError(f"terminals key {tid!r} != terminal_id {tcfg.terminal_id!r}")
        return self

    # --- convenience ------------------------------------------------------------------------

    @property
    def master(self) -> TerminalConfig | None:
        return next((t for t in self.terminals.values() if t.role is Role.MASTER), None)

    @property
    def slaves(self) -> list[TerminalConfig]:
        return [t for t in self.terminals.values() if t.role is Role.SLAVE]

    def assigned(self) -> list[TerminalConfig]:
        """Terminals we should hold a connection to: any with a role, and enabled."""
        return [t for t in self.terminals.values() if t.role is not Role.UNASSIGNED and t.enabled]

    def display_name(self, terminal_id: str) -> str:
        """The name to show this terminal by right now: the name set for whichever account it's
        last known to be logged into (see :func:`account_key`), else discovery's own ``label``,
        else the id's first eight hex characters -- the same last-resort the log and every
        notification already fall back to. One place for a fallback chain several call sites
        used to repeat by hand (``cfg.label or terminal_id[:8]``), so a rename shows up
        everywhere a terminal's name already does without hunting down every one of those call
        sites, only the one place this actually reads the name from.
        """
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
            cfg.terminals[d.terminal_id] = TerminalConfig(
                terminal_id=d.terminal_id,
                label=d.label,
                exe_path=d.exe_path,
                last_login=d.login,
                last_server=d.server,
            )
            log.info("new terminal discovered: %s (%s)", d.label, d.terminal_id[:8])
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
            if d.login is not None:
                existing.last_login = d.login
            if d.server is not None:
                existing.last_server = d.server
    return cfg


def load(path: Path | None = None) -> AppConfig:
    """Load config, returning defaults if the file is absent.

    A corrupt file is renamed aside rather than deleted, and defaults are returned, so a bad
    write can never leave the app unable to start.
    """
    target = path or config_path()
    if not target.exists():
        log.info("no config at %s, starting from defaults", target)
        return AppConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        cfg = AppConfig.model_validate(_migrate(raw))
        swept = _demote_orphaned_slaves(cfg)
        if swept:
            log.warning("slave(s) with no master found on load; unassigned: %s", swept)
            try:
                save(cfg, target)
            except OSError:
                log.exception(
                    "could not persist the orphaned-slave cleanup; will retry on next save"
                )
        return cfg
    except Exception as exc:
        backup = target.with_suffix(f".bad-{os.getpid()}.json")
        try:
            target.replace(backup)
            log.error("config at %s was invalid (%s); moved to %s", target, exc, backup)
        except OSError:
            log.error("config at %s was invalid (%s) and could not be moved aside", target, exc)
        return AppConfig()


def save(cfg: AppConfig, path: Path | None = None) -> Path:
    """Write config atomically.

    Writes to a temp file in the same directory then ``os.replace``, which is atomic on NTFS.
    A half-written config after a crash would be worse than a stale one.
    """
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = cfg.model_dump(mode="json", exclude_none=False)
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"

    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log.debug("config saved to %s", target)
    return target
