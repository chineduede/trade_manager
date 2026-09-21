# mt5copier

A multi-terminal MT5 trade copier in pure Python — no Expert Advisor is installed into any
terminal. One process discovers every MT5 install on the machine, you designate one as Master
and any number as Slaves, and the configuration persists.

**This version can send orders, and `dry_run` is what decides whether it does.** It defaults
to true, in which case the copier discovers, designates, connects, observes — and diffs the
master's open positions *and resting orders* to report what would be copied, and why anything
wouldn't be, sending nothing. Set it to false and those same decisions are dispatched to the
slaves.

Two guards sit underneath, and neither is optional: a slave is refused unless its account is a
**demo** (per-slave `allow_live_trading` overrides it), and a slave is not copied to until it
has been **reconciled** against the mapping table with no unexplained positions of ours on it.

**No order placed by this copier has been verified against a broker yet** — the wiring is
tested and the dry run is validated, but the one-slave demo run in the roadmap has not been
done. See [Roadmap](#roadmap) and [Detection](#detection-stage-2a).

## Running it

```bash
uv sync
uv run python -m mt5copier          # the TUI
uv run python -m mt5copier --list   # passive terminal list, connects to nothing
```

Or build the standalone exe:

```bash
uv run pyinstaller copier.spec --noconfirm
dist/mt5copier.exe
```

### Non-interactive flags

| Flag | Purpose |
|---|---|
| `--list` | Print discovered terminals and exit. Launches nothing, connects to nothing. |
| `--check` | Verify the MetaTrader5 extension imports. The main smoke test for a frozen build. |
| `--paths` | Print resolved config and log locations. |
| `--version` | Version. |

### Keys

| Key | Action |
|---|---|
| `m` | Make the selected terminal the Master (exclusive — demotes any previous master) |
| `s` | Toggle the selected terminal as a Slave |
| `u` | Unassign |
| `r` | Rescan for terminals |
| `c` | Ask the worker to drop and re-establish its MT5 session |
| `d` | Start/stop detection — the dry-run diff. Needs a master and at least one slave. |
| `ctrl+r` | Restart the worker process entirely |
| `ctrl+l` | Clear the detection ledger (forget every believed copy) |
| `F9` | **PANIC** — halt copying. Never closes or modifies a position. |
| `q` | Quit |

Clicking the `M` or `S` column of a row assigns that role directly.

## How it works

### One process per terminal

The `MetaTrader5` package keeps its connection in **process-global state**. `initialize()` and
every call after it operate on a single implicit session per process; there is no handle to pass
around and no way to hold two sessions at once. So N terminals require N OS processes — this is
not a design preference, it is the constraint the whole architecture follows from.

`engine.py` supervises one `multiprocessing` subprocess per designated terminal (spawn start
method, as Windows requires). Each child runs `worker.run_worker`, which owns exactly one
connection and publishes a `Snapshot` to a shared queue on every poll tick.

The engine itself runs on a thread inside the UI process and **never imports MetaTrader5** —
loading the extension in the parent would create a connection in the one process meant to hold
none.

### Discovery is passive, by necessity

The MT5 docs state: *"If required, the MetaTrader 5 terminal is launched to establish connection
when executing the initialize() call."*

There are 15 terminals installed on this machine. Probing them through the MT5 API to see what
is there would therefore **cold-start all 15**. So `discovery.py` touches only the filesystem
and the process list, and `initialize()` is called *only* for terminals you have explicitly
designated.

That turns out to cost almost nothing, because most of what the UI needs is readable off disk:

| Source | Yields |
|---|---|
| data folder name | `terminal_id` (32-hex instance hash) — the stable identity |
| `origin.txt` | install directory |
| install directory name | broker label |
| `config/common.ini` | last-used **Login** and **Server**, in plaintext (UTF-16) |
| `bases/<name>/` | trade servers this install has connected to |
| `psutil` | whether it is running right now, and its pid |

Only these genuinely require a live connection: balance, equity, free margin, currency,
leverage, open position count, `trade_allowed`.

`config/accounts.dat` is encrypted and is not parsed. **This app never handles MT5 passwords.**
It relies on each terminal already being logged in, which is why no credential field exists
anywhere in the config schema.

### Identity is the instance hash, not the path

Config is keyed on the 32-hex MT5 instance hash. A terminal upgrade or reinstall can move the
install path while the data folder — and therefore the logged-in account — stays put, so
path-keyed config would silently orphan your settings. A moved install is detected and logged
as a path update, with the role and risk settings preserved.

### Disconnected vs. stale

These are different failures and the UI shows them differently:

- **disconnected** — snapshots are still arriving, but the terminal has lost its broker link.
  Our plumbing is healthy; wait, or press `c`.
- **stale** — snapshots have *stopped* arriving. Our worker has gone quiet. Press `ctrl+r`.

Conflating the two is how a copier fails silently. Staleness is judged from the supervisor's own
arrival time rather than the worker's timestamp, because `time.monotonic()` is not defined to be
comparable across processes and wall clocks can step.

### Detection (stage 2a)

Press `d` and the engine starts diffing. It polls the master's open positions and compares them
against what it believes each slave already has, deriving opens, closes, partial closes and
SL/TP changes from the *difference* — it does not consume a stream of deals or order events. A
state diff is self-healing: a missed tick, a reconnect or a restart still lands on the correct
target state next comparison, where an event stream has to deliver every event exactly once or
it silently diverges.

Every decision is reported, including the decisions not to copy, because a copier that quietly
does nothing looks exactly like one that is broken. A skipped action is shown dimmed with its
reason — no stop loss on the master, symbol filtered, partials off for that slave.

#### It only copies what it watched happen

A position is copyable for a given slave only if that slave and the master were **both**
observed connected on the previous tick, no more than `join_grace_seconds` ago (default and
hard maximum 5 seconds). Anything else re-baselines that slave: whatever the master currently
holds becomes permanently uncopyable for it, reported once as `PREEXISTING`.

So a position already open when you press `d` is never copied, under any setting. Nor is one
the master opened while a slave's link was down for more than the grace window. The reason is
sizing honesty — lots come from the master's entry-to-stop distance, but a late joiner fills at
*current* price, so the real money at risk isn't the configured amount, and when the master is
already in profit it is silently *larger*. A three-second blip is different: a trade opened
moments ago fills within a whisker of the master's entry, which is what the grace window is for.

Positions already copied are never affected by a re-baseline. Their closes, modifies and
partials keep being mirrored, because those are real positions and someone has to manage them.

A slave also has to be *fresh*, not merely last-seen-`CONNECTED`. A worker that has gone quiet
is not copyable however healthy its last snapshot looked.

Two things Stage 2a deliberately left to later stages, both of which have since landed —
**this package can now send orders**, and `dry_run` is what stands between a decision and a
broker:

- **Sending.** `executor.py` is the only module that can cause an order to exist, and nothing
  reaches it while `dry_run` is true — which is the default. Underneath that, the executor
  refuses any account whose `trade_mode` is not demo unless `allow_live_trading` is set on
  that terminal specifically.
- **Lot sizes.** They need the slave's own terminal (`order_calc_profit`, and the symbol's
  volume step), so they are computed there. What the pure diff *does* decide is which basis
  applies, since that follows from pure data — and the log spells the choice out:

  | Master trade | Basis | Default |
  |---|---|---|
  | Has a stop loss | The slave's own risk, over the master's entry-to-stop distance, net of commission and slippage | on |
  | No stop loss | `no_stop_lot_multiple` × the master's lots | **off** (`no_stop_policy: skip`) |

  The no-stop path is the one case where a slave inherits the master's sizing. It exists
  because without a stop there is no risk distance to work from. Its risk is not the slave's
  configured risk and is not knowable in advance, and the copy opens unprotected — so it is
  opt-in per slave, and the log marks every such action `[UNPROTECTED]`.
- **Resolve symbols properly.** Only the configured prefix/suffix is applied. The
  `symbols_get()` fallback scan needs a connection, so it lands with Stage 2b.

#### Pending orders

Detected too, and gated per slave by `copy_pending` (default off). `PLACE`, `MODIFY_ORDER` —
an order's *entry price* can move, which a position's cannot — `CANCEL`, and `ORDER_FILLED`.

The one that matters is telling a **fill** from a **cancellation**: an order leaving the
master's list means one or the other, and the right responses are opposites. Cancelling a copy
that was about to fill drops the trade silently. MT5 gives the position opened by a pending
order that order's own ticket, so a departed ticket that now names a position was filled —
that is the signal used, and it is the one assumption here that only a live terminal can
confirm. Worth checking explicitly in your dry run.

Declining to mirror a resting order says nothing about the position it becomes: that is a new
event at a current price, judged on its own. So with `copy_pending` off, the resting order is
ignored and the resulting position is copied normally.

Stop-limit orders are not mirrored — they carry a trigger *and* an entry, and honouring one of
the two is worse than honouring neither.

#### Reverse copying

`invert` swaps stop and target rather than passing them through. The master's stop is where
its trade fails, which is where the reverse succeeds; unswapped, the reversed position's stop
would sit on the profitable side of entry. Two things follow: a reversed copy is sized to the
master's *take-profit*, so a master trade with no target inverts into an unstopped copy and
falls to `no_stop_policy`; and a reversed resting order flips kind as well as direction
(BUY_LIMIT ↔ SELL_STOP), because the price level is what has to be preserved.

Believed copies are persisted to `%APPDATA%\MT5Copier\dryrun_state.json` so that restarting the
app re-proposes nothing for positions it already tracked. It is bookkeeping, not settings:
deleting it (or pressing `ctrl+l`) is harmless, and costs one re-proposed action per open
master position. Stage 2c replaces it with the SQLite mapping table.

#### A decline is final

Once the copier decides not to open something — no stop loss on the master, a symbol filter,
or the position predating the watch — that ticket is never reconsidered for that slave.
Attaching a stop five minutes later does not make the trade copyable; it makes it a late join,
which is the same problem the exclusion rule exists to prevent.

Relatedly, there is no "master increased the position" case. An open position's volume can only
go down: on a hedging account a further entry is a new ticket, and arrives as an ordinary new
open. If a tracked position's volume is ever seen to *grow*, the log warns once and mirrors
nothing — it most likely means the master is a netting account, which would break the
partial-close ratios.

### Config

JSON at `%APPDATA%\MT5Copier\config.json`, written atomically (temp file + `os.replace`).

JSON rather than YAML is deliberate. The TUI is the primary editor, and round-tripping a
`model_dump()` through `yaml.dump` destroys every comment in the file — so a commented YAML
config that the app also writes will quietly eat your notes. JSON has no comments to lose; the
explanations live in this README instead. The file is indented and perfectly hand-editable.

A corrupt config is renamed aside (`config.bad-<pid>.json`) rather than deleted, and defaults
are used, so a bad write can never stop the app from starting.

The filtering, inversion and no-stop-policy fields (`no_stop_policy`, `invert`, `copy_sltp`,
`copy_partials`, `symbol_prefix`/`symbol_suffix`, `symbol_whitelist`/`symbol_blacklist`) are
read by the diff engine as of Stage 2a, as is `join_grace_seconds`.

The sizing fields are still unused, because lots are computed against the slave's own terminal
in Stage 2b:

| Field | Meaning |
|---|---|
| `sizing_mode` | `percent_of_balance` (default) or `fixed_cash_risk` |
| `risk_percent` / `risk_cash` | The risk budget per trade, from *this slave's* own balance |
| `commission_per_lot_round_turn` | Commission for one lot in **and** out, in account currency. Brokers quote per lot per side, so a "$3.50 commission" is $7.00 — enter the full there-and-back cost. Comes out of the risk budget before lots are derived. |
| `slippage_buffer_points` | Points of adverse fill to assume on entry and stop together, so the configured risk is a ceiling rather than a best case. |
| `no_stop_lot_multiple` | Only for `no_stop_policy: lot_multiple` — the multiplier on the *master's* lots. 1.0 mirrors its size exactly. Irrelevant to any trade that has a stop. |
| `max_lot` | Hard ceiling after quantization. The only bound left on an unstopped copy. |
| `max_deviation_points` | MT5's `deviation` on the order. Execution only — never an input to sizing. |

Carrying them all from the start avoids a migration the first time copying is switched on.

The schema is `extra="forbid"`, and a validation failure is treated as corruption, so removing
a field would otherwise reset every setting the user had. `_migrate()` strips retired keys
before validation instead — `default_sl_points` went that way in schema 2.

## Layout

```
src/mt5copier/
  paths.py           frozen-aware path resolution (sys._MEIPASS vs %APPDATA%)
  logging_setup.py   rotating file logs, no global-logger bug
  discovery.py       passive terminal enumeration
  config.py          pydantic v2 schema, merge, atomic save
  protocol.py        wire dataclasses, COPIER_MAGIC, DISPATCHABLE_KINDS
  worker.py          one process, one MT5 connection, reconnect loop
  reconcile.py       the diff engine: pure, MT5-free, decides but never sends
  sizing.py          risk -> lots, pure arithmetic
  symbols.py         master symbol -> slave symbol, pure, with a cache
  execution.py       filling modes and retcode classification, pure tables
  executor.py        the ONLY module that can create an order
  store.py           SQLite ownership: which slave position is whose copy
  engine.py          supervisor: spawn, aggregate, diff, reconcile, dispatch
  tui/app.py         Textual UI
launcher.py          frozen entry point (see note below)
copier.spec          PyInstaller spec
```

## Lessons carried over from `zele-mt5`

This project reuses the process-per-terminal pattern from `../zele-mt5`, and deliberately
avoids several defects found there:

- **`hiddenimports=["metatrader5"]` in lowercase.** PyInstaller's module graph is
  case-sensitive, so that pointed at a module which does not exist; the real C extension was
  never collected. Combined with building from the global interpreter instead of the project
  venv, its `warn-*.txt` listed 83 missing modules including `MetaTrader5`, `yaml` and
  `telethon` — the shipped 17MB exe could not have worked. Here the spelling is correct, the
  build runs under `uv run`, and `--check` verifies the result.
- **numpy.** `MetaTrader5` imports numpy from inside its C extension, so the module graph never
  sees the dependency. The exe builds clean, warns about nothing, then dies at runtime with
  `numpy._core.multiarray failed to import`. It is an explicit hidden import here.
- **No reconnect.** `Mt5Service` connected once in a constructor with no retry, no backoff and
  no health check, so a dropped connection killed the worker permanently. `worker.py` treats the
  connection as a loop with a backoff schedule.
- **Reads that tore down the session.** Its `get_acct_info()` called `mt5.shutdown()` when a
  read failed, destroying the process's connection as a side effect of a query. Here a failed
  read only marks state; teardown happens solely in the reconnect path. There is a regression
  test for this.
- **A global logger.** Its `get_logger()` cached one logger in a module global, so the first
  caller's name and file won for every later caller — masked only because each executor was a
  separate process. Plus no rotation.
- **CWD-relative paths.** `Path("data")` and `Path("app.yaml")` meant the exe silently failed
  when launched from a shortcut. `paths.py` is `sys.frozen`-aware.
- **`pydantic` undeclared**, arriving transitively via `sqlmodel`. It is a direct dependency here.

Also worth knowing if you reuse its `Mt5Service` in Stage 2: it hardcodes `ORDER_FILLING_IOC` in
all four order-send paths and never inspects `symbol_info.filling_mode`, and its volume rounding
uses `Decimal.quantize(step)`, which honours only the step's *exponent* — so `volume_step=0.05`
silently rounds to 0.01 granularity and can emit broker-rejected lots like `0.07`.

## Frozen-build notes

`launcher.py`, not `mt5copier/__main__.py`, is the PyInstaller entry script. PyInstaller runs
its entry script as top-level `__main__`, which discards package context and makes every
relative import in that module fail with *"attempted relative import with no known parent
package"*.

`multiprocessing.freeze_support()` runs before anything else. Under spawn, each worker
re-launches the exe; without it every worker would re-enter the UI and spawn workers of its own.

After **every** build, read `build/copier/warn-copier.txt`. Benign entries are POSIX-only
modules (`pwd`, `termios`, `fcntl`), typing-only stubs (`_typeshed`, `annotationlib`), optional
dev tooling (`mypy`, `cython`, `email_validator`), and attribute-style false positives such as
`missing module named pydantic.BaseModel`. Anything else deserves a look — then confirm with
`dist/mt5copier.exe --check`.

## Development

```bash
uv run pytest -q          # 415 tests, no MT5 terminal required
uv run ruff check .
uv run ruff format .
```

Tests use a fake MT5 module and synthetic `%APPDATA%\MetaQuotes\Terminal` trees, so the whole
suite runs without a terminal installed and without connecting to a broker.

## Roadmap

Stage 1 was the shell. Everything below is now written; **none of it has been run against a
broker**, which is what `ROADMAP.md`'s demo checklists are for and the only thing that turns
"built" into "works".

1. ~~**Copy engine** — master position polling and master→slave reconciliation.~~ `reconcile.py`.
2. ~~**Filling-mode negotiation**~~ — probes `symbol_info.filling_mode` (`& 1` FOK, `& 2` IOC)
   per symbol with a cache and a fallback retry. Without this, FOK-only symbols reject every
   order with `TRADE_RETCODE_INVALID_FILL`.
3. ~~**Retcode-aware retry**~~ — `REQUOTE`, `PRICE_OFF`, `INVALID_FILL`, `NO_MONEY`,
   `DONE_PARTIAL` and a `None` result, with re-pricing rather than one-shot give-up.
4. ~~**Step-correct volume quantization**~~ — `(v / step).to_integral_value(ROUND_DOWN) * step`.
5. ~~**Risk-based sizing**~~: percent of the slave's own balance (default), or a fixed cash
   amount per trade, both derived from the master's stop distance via `order_calc_profit` and
   net of commission and slippage. A slave never inherits the master's position size — with one
   deliberate exception: a master trade with **no stop** has no risk distance, so
   `no_stop_policy: lot_multiple` sizes it off the master's lots instead. That is off by
   default. See `ROADMAP.md`.
6. ~~**Symbol mapping**~~ — broker suffix *and* prefix handling, cached per `(terminal, symbol)`.
7. ~~**SL/TP sync and proportional partial closes.**~~ Partials with Stage 2c, the two
   modifications with Stage 3. A modification does not go through the order-sending path: it
   has no price to re-quote and no volume to fill, and MT5 answers it with zeroes.
8. ~~**Pending orders**~~ — placement, modification and cancellation, per slave via
   `copy_pending` (default off).
9. ~~**Reverse and filtered copying**~~ — per-slave inversion, symbol whitelist/blacklist.
   Inversion swaps stop and target rather than flipping them; see `ROADMAP.md` for why.

Panic semantics are fixed now and will not change: it halts copying and never closes or modifies
a position.
