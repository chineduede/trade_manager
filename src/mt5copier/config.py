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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .discovery import DiscoveredTerminal
from .logging_setup import get_logger
from .paths import config_path

log = get_logger(__name__)

SCHEMA_VERSION = 2

_REMOVED_TERMINAL_FIELDS = ("default_sl_points",)
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


class TerminalConfig(BaseModel):
    """Per-terminal settings. Survives rescans; only the discovery-derived fields get refreshed."""

    model_config = ConfigDict(extra="forbid")

    terminal_id: str
    label: str = ""
    exe_path: Path | None = None
    role: Role = Role.UNASSIGNED
    enabled: bool = True

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

    symbol_prefix: str = ""
    symbol_suffix: str = ""
    symbol_whitelist: list[str] = Field(default_factory=list)
    symbol_blacklist: list[str] = Field(default_factory=list)

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

    dry_run: bool = True
    """Detect and report intended actions, but send nothing to any broker.

    Defaults to true and stays true for the whole of Stage 2a, where no execution code exists
    at all -- the engine refuses to act on a false value it cannot honour rather than appearing
    to go live. Turning it off has to be a deliberate act, taken after the dry-run log has been
    read and believed."""

    terminals: dict[str, TerminalConfig] = Field(default_factory=dict)

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

    def set_role(self, terminal_id: str, role: Role) -> None:
        """Assign a role, demoting any existing master so the one-master rule always holds."""
        if terminal_id not in self.terminals:
            raise KeyError(terminal_id)
        if role is Role.MASTER:
            for tcfg in self.terminals.values():
                if tcfg.role is Role.MASTER and tcfg.terminal_id != terminal_id:
                    tcfg.role = Role.UNASSIGNED
        self.terminals[terminal_id].role = role


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
        for field_name in _REMOVED_TERMINAL_FIELDS:
            if entry.pop(field_name, None) is not None:
                log.info("dropped retired config field %r", field_name)
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
        return AppConfig.model_validate(_migrate(raw))
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
