# Web UI plan

Not started. This is the plan for the web front end agreed on 2026-10-01, written before any
code so the decisions are explicit instead of accidental. `ROADMAP.md` stays the plan for
trading logic; this stays the plan for the web presentation layer. Cross-referenced, not merged
— the web UI has no trading-logic decisions of its own, only UI-shaped ones.

## Sequencing (decided 2026-10-01 — do not reorder without asking)

1. **Finish `ROADMAP.md`'s "Live verification backlog" on the current TUI first.** Nothing here
   is blocked on this document, and nothing in this document should be started before that
   backlog is checked off. Shipping a nicer way to drive code that has never been proven to
   refuse a live account correctly just makes the eventual bug harder to isolate.
2. **Build the web UI** (this plan).
3. **New trading features (breakeven, prop-firm guards, "and the rest") are designed and built
   after the web UI exists, and their UI lands only in the webpage — the TUI does not gain
   them.** Decided explicitly, not a default: this means the TUI becomes a frozen, proven
   baseline rather than the ongoing primary interface. The trading *logic* for those features
   still goes through the same policy-then-build-then-demo-verify process as everything already
   in `ROADMAP.md` — the web UI changes where the controls live, not how new trading rules get
   trusted.

## Non-negotiables carried over from the TUI/engine — the UI must not weaken these

- **The demo-only guard is unaffected** — it lives in `executor.py`, below any UI, and stays
  there.
- **The panic switch (`F9` in the TUI) needs an equally prominent, always-reachable control on
  every page** of the web UI. Its meaning (halts copying, never touches positions) is fixed per
  `ROADMAP.md` and carries over unchanged.
- **Live-override arming keeps its friction.** The TUI requires typing the terminal's full name
  to turn it on; a web "Arm" button with a confirm dialog is not an equivalent and must not
  replace it. Port the same typed-name requirement.
- **The orphan-confirmation flow still blocks** before a settings save that would orphan open
  copies — ported as a blocking step in the web flow, not a dismissible toast.
- **`dry_run` defaults on** for anything that can newly touch an account; the web UI inherits
  this default, it does not get to relax it for convenience.

## Architecture

### One `Engine`, one process

The web server runs **inside the same process as the `Engine`**, not a second instance and not
over IPC to a second process. `Engine` already guards its state with `self._lock`, so this is
safe without new synchronization work, and it avoids a split-brain where the TUI and a web
server each believe something different about what is running.

**v1 is exclusive mode:** `mt5copier --web` starts the `Engine` and the web server instead of
the TUI, the same pattern as today's `--list` / `--check` flags. Running the TUI and the web
server concurrently against one `Engine` is feasible later — the locking already supports it —
but it doubles the surface for the first security-sensitive release (two renderers, two things
to reason about touching live trading at once) and is deliberately deferred, not ruled out.

### Backend: FastAPI + Jinja2 + htmx, not a SPA

- **No Node/build pipeline.** The project ships as one PyInstaller exe (`copier.spec`); templates
  and static assets become `datas` in the spec, with no `npm` step added to the release process.
- **Most of the surface is CRUD-shaped forms** — settings editor, live-override, orphan
  confirm, role assignment. htmx (partial HTML swaps over POST) fits that directly; it does not
  need a client-side state store the way a SPA would for the same handful of forms.
- **FastAPI's websocket support gives the live snapshot stream for free**, and its async model
  fits the engine's existing polling/queue design better than a second REST-polling loop would.
- `pydantic` is already a dependency (FastAPI's native validation layer), so this is additive,
  not a second validation story.
- If a later feature genuinely needs rich client interactivity (a chart, drag-resize), that one
  page can load vanilla JS / a bundled Chart.js file — it does not have to pull the whole app
  into a SPA.

### Security — required for v1, not a follow-up

Full control is reachable over HTTP in this design, so these are not optional hardening passed
for later; they ship with Web Stage A.

- **Bind to `127.0.0.1` only.** No config path to `0.0.0.0` without a separate, deliberately
  awkward flag — mirrors the live-override naming-friction pattern on purpose.
- **Token auth, Jupyter-notebook-style.** A random token generated at startup (or persisted to
  `%APPDATA%\MT5Copier\web_token` with restrictive ACLs), required on every API/websocket call;
  printed once at startup as a clickable `http://127.0.0.1:<port>/?token=...` URL. A page open
  in another tab without that token gets nothing back.
- **CSRF defense that doesn't rely on the cookie alone.** Every state-changing call must also
  carry a custom header (e.g. `X-MT5Copier-Token`). A cross-site `<form>` POST cannot set a
  custom header, and `fetch`/XHR cannot either without a CORS preflight — which this server
  never approves (no `Access-Control-Allow-Origin` issued, ever, for any origin).
- **Default-deny CORS.** Page and API are same-origin by construction (one FastAPI app serves
  both); nothing opts back into cross-origin access.
- **State-changing actions are POST-only, never GET.** Closes the classic
  `<img src="http://127.0.0.1:PORT/api/panic">` class of attack outright.

## Staged build

Same culture as `ROADMAP.md`'s staging — each stage is small enough to demo and get sign-off on
before the next starts.

**Web Stage A — read-only dashboard.** Terminal/worker status, live positions & equity over the
websocket, log tail. No write path at all. Lowest risk; proves the FastAPI + htmx + websocket
plumbing and the full auth/CSRF scaffolding *before* anything that can move money is reachable
over HTTP.

**Web Stage B — settings & roles.** Per-terminal settings form and role assignment, calling the
*same* `settings_edit.py` / `Engine.apply_settings` the TUI already uses — no parallel
validation logic written for the web layer. Orphan-confirmation ported as a blocking step.

**Web Stage C — the two highest-friction controls.** Live-override and panic, ported last and
deliberately, with their existing friction preserved exactly (typed terminal name to arm; an
unmissable panic control on every page, not buried in a menu).

**Web Stage D — new trading features.** Breakeven and prop-firm guards get their policy
questions settled first, in writing, the same way Stage 3's policy questions were settled in
`ROADMAP.md` — then their engine-side logic is built and demo-verified like every other trading
rule. Stages A–C only move *where* the controls live; they do not let D skip verification.

## Decided 2026-10-01 — the two open calls from the first draft

- **Frontend: htmx, not a full SPA.** Confirmed. No Node/build step, no second packaging path
  — templates and a vendored (not CDN-loaded, so the frozen exe works offline) `htmx.js` ship as
  `datas` in `copier.spec` alongside the existing PyInstaller build.
- **Auth: a fresh URL-token per run, Jupyter-style.** Confirmed over a one-time password. No
  setup step, no "where did I put that password" — fits "full control, same machine, just
  whoever's at the keyboard."

Both decisions are now locked into the Stage A scope below; re-open only by asking first, same
rule as the sequencing decision above.

## UI feel — decided 2026-10-02: trading-terminal style

Chosen over "terminal-kin" (a CLI-aesthetic dark theme matching the Textual TUI) and a light,
card-based SaaS-admin look: dark, dense, ticker-like — ticket data (lots, SL/TP, P&L) read as
market data, not as log lines. Colors and ink come from the data-viz skill's validated reference
palette (`references/palette.md`), not ad hoc hex — this page is a status dashboard, so only its
**status** and **chrome/ink** tokens apply; there is no multi-series chart here needing the
categorical eight.

```css
:root {
  color-scheme: dark;
  --surface:         #1a1a19;   /* panel/row background */
  --page:            #0d0d0d;   /* page background */
  --ink-primary:     #ffffff;
  --ink-secondary:   #c3c2b7;
  --ink-muted:       #898781;   /* unassigned / stopped — nothing to alarm about */
  --hairline:        #2c2c2a;   /* row dividers */
  --status-good:     #0ca30c;   /* connected; P&L positive */
  --status-warning:  #fab219;   /* starting / connecting / stopping; a held slave */
  --status-serious:  #ec835a;   /* stale; disconnected */
  --status-critical: #d03b3b;   /* worker dead / failed; detection blocked; P&L negative */
}
```

All four status steps clear the skill's >=3:1 mark-contrast floor on this exact dark surface
(`#1a1a19`): good 5.19, warning 9.49, serious 6.60, critical 3.62 — no re-validation needed since
these are the documented instance's own dark-surface numbers, not new hues.

**Status tier maps onto this project's own existing vocabulary** (`TerminalView.status()`,
`protocol.WorkerState`, `engine.holds()`, `engine.blocked_reason`) rather than inventing a
parallel one:

| Engine state | Tier |
|---|---|
| `WorkerState.CONNECTED` | good |
| `STARTING` / `CONNECTING` / `STOPPING` | warning |
| held (`engine.holds()` has an entry for this terminal) | warning |
| `stale` (worker alive, gone quiet) / `DISCONNECTED` | serious |
| `worker dead` / `FAILED` | critical |
| detection blocked (`engine.blocked_reason` is set) | critical |
| `-` / `not connected` / `STOPPED` | muted ink, no status color |

**P&L reuses `--status-good`/`--status-critical` directly** rather than a separate green/red
pair — this is exactly the skill's collision rule ("when a series means good/bad, it wears
status tokens"), not a second invented color pair to keep in sync with the first.

**Role (MASTER/SLAVE) is never color-coded.** It's identity, not state — plain text/icon badge
in `--ink-secondary`. There's no categorical palette in play here at all for Stage A (no
multi-series chart), so there's nothing to assign a hue to in the first place.

**Typography:** system sans everywhere (`system-ui, -apple-system, "Segoe UI", sans-serif`,
the skill's default — no separate monospace stack). `font-variant-numeric: tabular-nums` on the
lot/price/P&L columns only, since those are exactly "columns that must align vertically" per the
skill's rule — gets the dense ticker look the chosen preview showed without a second type family.

**Status color never appears without an icon + label**, matching the skill's own mitigation
rule for its sub-3:1 light-surface steps — moot on a dark-only surface today, but keeping the
discipline now means a light variant later (if ever wanted) doesn't need an accessibility
redesign on top of a cosmetic one.

**Dark-only for v1, by decision, not an oversight.** A trading-terminal look reads as inherently
dark (this is the same register as a broker platform's own dark chart, not a document that gets
a light/dark toggle); add a light variant later only if asked.

## Web Stage A — detailed scope (decided 2026-10-01, not yet built)

Read-only: terminal/worker status, live snapshot data, and a log tail, over one websocket per
client. No POST route exists anywhere in this stage — nothing here can touch a position, so
Stage A's CSRF story is simply "there is no write path to protect yet"; the custom-header
defense described under Security applies starting Stage B, when the first POST appears.

### Push model: server-rendered fragments over one websocket, not a JSON API

`WEBUI_PLAN.md`'s architecture note said "FastAPI's websocket support gives the live snapshot
stream for free" — made concrete, that means **htmx's websocket extension** (`htmx-ext-ws`,
vendored alongside `htmx.js`), not a hand-rolled JS client parsing JSON. The server pushes
pre-rendered HTML fragments down the socket; htmx swaps each into the DOM out-of-band by id.
This keeps Stage A's only JS to "load htmx + its ws extension" — no bespoke client-side
rendering logic to keep in sync with the Jinja2 templates.

- `index.html` — the dashboard shell: a health/blocked-reason banner (`#health-bar`), the
  terminal table (`#terminal-table-body`), a log tail panel (`#log-tail`). Opens the websocket
  on load via `hx-ext="ws"` / `ws-connect="/ws/state?token=..."`.
- Per tick (0.5s, matching the TUI's own `_on_timer` cadence — no reason to push faster than the
  UI it's modeled on), the server renders and sends:
  - `#health-bar` — from `engine.health()` plus `engine.blocked_reason` and `engine.holds()`.
  - One fragment **per terminal row** (`row-<terminal_id>`), not the whole table — mirrors
    `tui/app.py`'s own "only rebuild rows when the set of terminals changes, else update rows in
    place" discipline (`_rebuild_rows` vs. per-tick updates), so a 10-terminal dashboard doesn't
    re-render 9 unchanged rows every half second.
  - `#log-tail` — only the lines appended since that connection's last push (track a byte offset
    per websocket connection), not the whole tail re-sent every tick.
- `engine.views()` already returns copies made under `self._lock` specifically so a caller can
  hold them across its own event loop without racing the drain thread (its own docstring says
  so) — this is exactly that caller.

### New module: `src/mt5copier/web/`

- `app.py` — `create_app(engine: Engine, token: str) -> FastAPI`. Lifespan handler calls
  `engine.start()` on startup / `engine.stop()` on shutdown — the same two calls
  `TUICopierApp.on_mount` / `on_unmount` already make, just moved behind FastAPI's lifespan
  instead of Textual's mount hooks.
- `auth.py` — token check via `secrets.compare_digest`, never `==` (timing). The initial page
  load takes the token as `?token=` (a browser's first `GET /` has no header yet); from there,
  every subsequent fetch/websocket-connect carries it explicitly. Browsers cannot set a custom
  header on a WebSocket handshake, so `/ws/state` takes the token as a query param too — this is
  Stage A-only and does not set precedent for Stage B/C's POST routes, which still get the
  custom-header defense as planned.
- `routes.py` — `GET /` (the shell, token-gated), `WS /ws/state` (the push loop above). Nothing
  else; a `GET /api/...` JSON API is deliberately not added since nothing in this stage needs
  one.
- `templates/` + `static/htmx.min.js` + `static/htmx-ws.min.js` — vendored, not CDN-fetched.

### Server lifecycle (`mt5copier --web`, new flag in `__main__.py`)

Same shape as today's `--list`/`--check`: acquires the existing `SingleInstanceLock(lock_path())`
before doing anything else — a web run is still only one `Engine`, same as a TUI run, not a
second kind of instance to race `config.json`. Generates `secrets.token_urlsafe(32)`, prints
`http://127.0.0.1:<port>/?token=...` once, then `uvicorn.run(app, host="127.0.0.1", port=port)`
— `127.0.0.1` is written directly into this call, not read from a config field, so there is no
path to `0.0.0.0` short of editing source.

### Testing — no broker needed, same as the other 723

- `test_web_auth.py` — missing/wrong token refused (page load and websocket handshake both);
  correct token admitted.
- `test_web_routes.py` — FastAPI's `TestClient` against `create_app()` wired to a fake `Engine`
  (same fakery pattern `test_tui.py` already uses) — dashboard reflects known terminals and
  `engine.health()`'s counts.
- `test_web_push.py` — a websocket test client: connect, assert the expected fragments arrive
  on a tick; two concurrent connections each get their own log-tail offset and neither starves
  the other.

### Packaging

`copier.spec` gains `templates/` and `static/` as `datas`. `fastapi`, `uvicorn`, `jinja2` join
`pyproject.toml`'s `dependencies`. Flag, not a known issue yet: uvicorn's import-time plugin
discovery has tripped up PyInstaller's static analysis on other projects before — worth a
`--check`-style smoke import test once this is built, the same reasoning `_cmd_check()` already
applies to the `MetaTrader5` extension.

### Explicitly out of scope for Stage A

Every write path — settings editor, live-override, panic, orphan-confirm — stays Stage B/C
exactly as `WEBUI_PLAN.md` already sequenced. Stage A proves the plumbing (auth, websocket push,
packaging) before anything reachable over HTTP can move money.

## Web Stage A — built and verified 2026-10-02

Built as scoped above, under `src/mt5copier/web/` (`auth.py`, `presentation.py` — the pure view
model, `routes.py`, `app.py`, `server.py`, `templates/`, `static/`), wired in via `mt5copier --web
[--port N]` (default port `8765`) in `__main__.py`. 25 new tests (`tests/test_web_*.py`), 748
total, `ruff`/`ruff format`/`mypy` all clean.

**The uvicorn/PyInstaller packaging risk flagged above did not materialize.**
`pyinstaller-hooks-contrib` (already a transitive dependency of the project's `pyinstaller`) ships
`hook-uvicorn.py`, `hook-websockets.py`, `hook-anyio.py`, `hook-pydantic.py` and `hook-jinja2.py`
out of the box — a real `uv run pyinstaller copier.spec --noconfirm` build picked up uvicorn's
dynamic loop/protocol selection without the explicit `hiddenimports` added for it being
load-bearing (kept anyway, as defensive documentation of what uvicorn does at import time, not
because the build needs them). `warn-copier.txt`'s long "missing module" list is the expected
cross-platform noise (POSIX-only modules, optional `uvloop`/`trio`/`gunicorn` fallbacks neither
installed nor needed on Windows) — read in full per the spec file's own instruction, nothing in
it is a real gap.

**Verified against the actual frozen exe, not just the source tree:** `mt5copier.exe --web
--port 8798` against an isolated `%APPDATA%`, then `curl` against `/` (401 without a token, as
designed), `/static/htmx.min.js`, `/static/htmx-ws.js`, and `/static/style.css` (all 200) —
confirms the vendored JS/CSS and Jinja2 templates actually resolve at runtime inside the bundle,
not just on disk during development. Also re-ran the unfrozen dev server end to end: real config
load, real (empty) discovery sweep, the printed `http://127.0.0.1:<port>/?token=...` URL, and the
rendered dashboard showing the real `copier.log` tail.

**One real decision made during the build, not pre-specified:** floating P&L is shown at the
*account* level (`snapshot.equity - snapshot.balance`), not per position. `protocol.Position` was
deliberately built without a live `profit` field — its own docstring says keeping it to the
stable subset `positions_get()` returns is what lets two snapshots compare equal when a position
is unchanged "instead of differing because the floating P/L moved." Per-position P&L would mean
either reopening that decision or fetching it out-of-band from the reconcile path, neither scoped
here — account-level equity-vs-balance uses data already on `Snapshot` and still gives the
good/critical P&L coloring the UI feel calls for.

### Redesign pass, 2026-10-02 — the first cut's visual design was weak, named and fixed

The first version that shipped above was a bare table with a colored dot for status and nothing
else — flagged by the user as "extremely horrible," correctly. htmx was not the cause (it has no
visual opinion at all; it only decides how fragments get swapped in) — the CSS/HTML itself was
thin. Concrete gaps, and what replaced them:

- **Status was hue-only** (a colored dot, no shape) — the exact thing the data-viz skill's own
  rule warns against ("a status color never appears without an icon + label"). Replaced with
  real status icons: five SVGs (`circle-check`, `triangle-alert`, `octagon-alert`, `circle-x`,
  `circle-minus`) pulled once from Lucide (ISC license) and inlined in a new
  `_status_icon.html` macro — `stroke="currentColor"` lets the existing `tier-*-text` color
  classes drive the icon's color, so there is exactly one source of truth per tier, not two.
  This is the "library" explored per the user's "perhaps we can explore libraries if needed" —
  a vendored icon set with zero runtime dependency, not a CSS framework (a generic framework
  would have fought the already-validated dark palette more than it helped).
- **The log panel was a flat gray dump.** `presentation.render_log_lines()` now classifies each
  line by its level (parsed from the log format's own `%(levelname)-7s` field) and wraps it in a
  `<span>` colored by the same status tokens, so a WARNING or ERROR is visible without reading
  every line.
- **No visual weight on the master row** — the single fact that matters most wasn't emphasized
  at all. `tr.row-master` now gets a left accent border and bolded label.
- **Role was plain uppercase text**, indistinguishable from a regular cell. Now a bordered
  badge, explicitly *not* colored with status hues (role is identity, not state — reusing a
  status color for it would be exactly the collision the data-viz skill's rule warns against).
- **No `max-width`** — the page stretched edge-to-edge on a wide monitor. Added a centered
  `.shell` container, plus a proper page header (title + subtitle) and a stat-tile health bar
  instead of one run-together line.
- **The "HELD" column was empty dead space** for almost every row. Folded into the status cell
  instead of living in its own mostly-blank column.
- **Two small live-feeling touches**, both plain vanilla JS (`static/dashboard.js`), no
  framework: the log panel auto-scrolls when the viewer is already at the bottom and caps
  retained history at 400 lines so a long-running session doesn't grow the DOM forever; any
  element htmx swaps in out-of-band gets a brief CSS flash so an update away from where the
  viewer is looking is still noticeable. Both are built on `MutationObserver`, a stable standard
  DOM API, deliberately **not** on htmx's own swap-lifecycle events — this session's environment
  has no way to open a real browser and confirm the exact payload shape of htmx's internal
  events, so the implementation leans on an API that can be reasoned about directly instead.
  **Caveat, stated plainly: none of this redesign was seen rendered in an actual browser** — no
  browser/screenshot tool was reachable in this session. Verification here is structural only:
  the rendered HTML was inspected directly (ids, classes, icon markup, log-level classes all
  present and correctly derived) against both synthetic data and a real live run that discovered
  real terminals on this machine, and the full test suite (quoted below) still passes. Treat the
  visual result as unverified until actually looked at in a browser.

All 748+ tests still pass after this pass (4 new ones added for `render_log_lines`), `ruff`,
`ruff format`, and `mypy` all clean. No `copier.spec` change needed — the new `dashboard.js` and
`_status_icon.html` files land inside the existing directory-level `datas` entries for
`templates/` and `static/`, not named individually.

**The caveat above was immediately justified: the user opened it in a real browser and the
whole page flashed continuously.** Root cause: `ws_state()` sent the health bar and every row on
*every* tick, unconditionally, regardless of whether anything had changed. `hx-swap-oob="true"`
replaces the whole element with a fresh DOM node, so this meant a brand-new node for the health
bar and every row twice a second, forever — and `dashboard.js`'s flash-on-update affordance
fires on any added node with an id, so it dutifully flashed all of it, continuously. Fixed by
tracking the last fragment actually sent per connection (seeded from the engine's current state
right after `accept()`, since the index page this connection follows already rendered that same
state moments ago) and only sending a fragment whose rendered HTML differs from what was last
sent — the same "only touch what changed" discipline `tui/app.py`'s `_rebuild_rows` already
follows for the Textual UI, ported to the websocket. Two existing tests had encoded the bug as
an assumption ("every tick sends health then one row") and needed rewriting along with the fix;
a new regression test (`test_unchanged_health_and_row_are_never_resent`) asserts the opposite.

## Stage A rebuilt, 2026-10-02 — architecture and visual feel both reopened

Even after the flashing fix above, the user said the redesign was still lacking, that htmx
might be falling short, and asked for three things explicitly: "use urls for resources" (not one
page pushed over a socket), a different visual direction ("minimalistic and retro," landing on
pixel/8-bit after a round of concrete previews), and genuine responsiveness, which the first two
passes never added. This is the second time a decision here has been reopened after shipping —
both times by asking first, per this document's own rule, not assumed.

**Websocket push is gone. htmx stays, but as polling against per-resource routes.** The user's
first instinct was "a real JSON API + separate navigable pages," but confirmed afterward that if
htmx alone can cover "use urls for resources" with no JSON needed, stick with htmx — so there is
still no JSON API and still no hand-rolled JS client parsing one. What changed is the push model:

- **Three real pages, not one:** `GET /`, `GET /terminals`, `GET /log`, each its own
  server-rendered Jinja2 template extending a shared `base.html` (nav + shell), not three
  sections of one screen.
- **Each page's live panel polls its own partial route** — `GET /partials/health`,
  `/partials/terminals`, `/partials/log` — via a stable element carrying
  `hx-get` + `hx-trigger="every Ns"` + `hx-swap="innerHTML"`. This satisfies "use urls for
  resources" directly: each resource genuinely has its own URL now, fetchable on its own.
- **No self-replacing "outerHTML swap of self."** The htmx polling idiom that swaps the whole
  trigger element (re-including its own `hx-get`) exists mainly so a response can *stop* polling
  by omitting the trigger — not needed here, and it would have broken the log panel's
  auto-scroll: `innerHTML` replaces only a stable element's *children*, so the same DOM node
  keeps existing (and keeps polling, and keeps being watchable by a `MutationObserver`) forever;
  an outerHTML self-swap would hand that observer a new node it was never watching.
- **No more byte-offset tracking for the log.** Each poll just re-reads and re-renders the last
  200 lines and the panel's content is fully replaced — simpler than the previous push design's
  incremental-append bookkeeping, at the cost of not literally only sending *new* bytes. Given
  the log panel polls independently (every 3s) rather than being pushed on a shared 0.5s tick,
  this trade reads as a clear simplification, not a regression.
- **`dashboard.js` is now one job, not two.** The flash-on-update affordance is gone along with
  the websocket it was built for (there is no more "every fragment resent on every tick" failure
  mode to flash against, by construction, since polling only ever fetches one resource's current
  state on its own schedule). What's left: stick-to-bottom auto-scroll on the log panel, via
  `MutationObserver` on the one stable, never-replaced log element — the same "lean on a stable
  standard DOM API, not on htmx's internal event payload shapes" discipline as before, now
  additionally justified by needing a node that survives every poll.

**Visual feel: pixel/8-bit retro, replacing trading-terminal dark.** Decided via concrete ASCII
previews (green-phosphor CRT, amber data-terminal, quiet minimal, pixel/8-bit — the user picked
pixel/8-bit) rather than another round of guessing. What changed:

- **Status badges are bracketed text, not an icon or a dot:** `[OK]` / `[WARN]` / `[ERR]` /
  `[DEAD]` / `[OFF]`, via `presentation.badge_for_tier()`. This satisfies the same accessibility
  rule the SVG icons were chasing (a status color never carries meaning by hue alone) without
  needing an icon library at all — text reads as its own tier regardless of color vision, and it
  fits a retro terminal status line better than a smooth SVG glyph would have next to it.
- **No border-radius anywhere; hard 3px borders with a flat, non-blurred offset shadow**
  (`box-shadow: 4px 4px 0 #000`) on every panel and nav link — the classic retro-UI "pixel
  bevel." The current-page nav link collapses its shadow and nudges into where the shadow was,
  simulating a pressed button.
- **Two vendored pixel fonts, not Google Fonts CDN** (same "works offline" discipline as htmx):
  Press Start 2P (OFL) for the page title only — it is close to unreadable at body size — and
  VT323 (OFL) for everything else, a pixel-terminal font that stays legible at table-row density.
  Both fetched once from `fonts.gstatic.com` and committed as `.woff2` files under `static/`.
- **The validated status/ink color tokens are unchanged.** Pixel/retro is a shape and typography
  language layered on top of already-accessible colors, not a reason to re-derive them.
- **Real responsive breakpoints**, added for the first time (neither earlier pass had any): a
  `max-width` shell with shrinking padding, a horizontally-scrollable table wrapper, and a
  `640px` breakpoint that reduces font sizes and padding — standard, well-established patterns
  chosen because they could be reasoned about correctly without a browser to test them in,
  deliberately not anything more elaborate (e.g. a card-based table transform) that would carry
  more risk of being wrong unseen.

**Tests rewritten for the new routes, not just patched.** `test_web_push.py` (the websocket)
is gone; `test_web_auth.py` (tests `TokenAuth` directly, independent of routes) needed no
changes. `test_web_routes.py` now covers all three pages and three partials, including that a
partial returns *only* its fragment — not a full page, which would otherwise nest the whole
page inside itself on every poll. Fixing these up front caught a real latent bug in the test
fixture itself: `received_at=0.0` always read as stale, because `time.monotonic()` on this
machine starts in the millions, not near zero — masked for several commits by an assertion
(`"connected" in resp.text`) that is trivially true whenever "not connected" is on the page too,
since it's a substring of it. Fixed the fixture and tightened the assertions that had been
silently proving nothing.

**Same caveat as before, because the facts haven't changed: none of this has been seen in a
real browser.** Verified structurally — the real server was run end to end (real config load,
real discovery, all three pages and all three partials returned 200, both vendored fonts served)
and the rendered HTML inspected directly for correct nav highlighting, correct badges, and
correct polling attributes. 763 tests pass, `ruff`/`ruff format`/`mypy` clean. The visual result,
and whether the pixel aesthetic and responsiveness actually read the way they're meant to, is
still only verifiable by opening it.

## Stage A, third visual/structural pass -- 2026-10-02

The sidebar rebuild above was still "lacking... dull" and "not easily extensible" per the user,
after two redesigns already. Asked with concrete ASCII previews again rather than guessing a
third time (ASCII preview, both a visual-direction and a nav-shape question, `AskUserQuestion`):
**pixel retro, intensified** (not abandoned) and **a sidebar with a reserved, pinned-bottom slot
for Stage C's panic control** (not a wider top nav, not an overflow menu).

**Extensibility was a real code smell, not just a feeling -- found concretely in `routes.py` and
`base.html` before changing anything:**
- Every page used to be copy-paste: a route + a hand-written `<a href="...?token=...">` added to
  `base.html`'s nav by hand for each one. Fixed with `routes.py`'s new `NavItem`/`NAV` list --
  one place that knows what pages exist; `base.html` just loops over whatever `nav` it's given.
  Stage B/C add `NavItem("settings", "SETTINGS", "/settings")` etc. to that one list and nothing
  in the template changes.
- Each page route rebuilt the same `{"token":..., "page":...}` context dict by hand. Replaced
  with a `_page()` helper in `routes.py`'s closure so a route body only supplies what's actually
  page-specific (`health=...`, `rows=...`, `log_text=...`).
- The top nav had no structural room for an "always-reachable panic control" (a
  `WEBUI_PLAN.md` non-negotiable for Stage C). `base.html` now has an empty
  `{% block nav_footer %}{% endblock %}` inside a sidebar slot pinned to the bottom via
  `margin-top: auto` -- Stage C overrides that one block and the control lands in a fixed,
  on-every-page position with no other template touched. **Left genuinely empty for now, on
  purpose** -- a dead/fake panic button would be worse than no button on a tool that moves
  money, so nothing fake was added just to "show" extensibility.

**Visual: pixel retro kept, not replaced, per the user's pick.** What changed, scoped exactly to
what was asked (grayscale+status elsewhere stays untouched):
- One new decorative token, `--accent: #4dff8c` (a brighter phosphor green than the validated,
  deliberately muted `--status-good`) used in exactly two places: the blinking title caret
  (`.caret`, `@keyframes caret-blink`, respects `prefers-reduced-motion`) and a glow
  (`box-shadow`) on `.badge-good` only -- the tier a viewer is scanning a table for. It carries
  no status meaning and must never be reused as a status color; doing so would collide with the
  data-viz skill's status/ink separation that every earlier pass was careful to preserve.
- A subtle, fixed, `pointer-events: none` CRT scanline overlay (`repeating-linear-gradient`,
  ~3.5% opacity) over the whole viewport -- decorative texture, confirmed not to block clicks
  since `pointer-events: none` removes it from hit-testing regardless of stacking order.
- No other color, motion, or texture changed -- intentionally, per the user's own pick of
  "intensified," not "redesigned again."

**Structural layout: top nav replaced by a sidebar.** `.app-shell` (flex row) holding
`.sidebar` (fixed `--sidebar-width: 220px`, the brand/title, the nav list, the pinned footer
slot) and `.main` (the page content, unchanged panels/tables/log). Below 640px the sidebar
flips to a horizontal top bar (`flex-direction: row`, wrapping) rather than a collapsible
drawer -- deliberately the same "well-established pattern reasoned about without a browser"
discipline as the first retro pass's breakpoints, not a hamburger-menu-plus-JS-toggle that would
need a real browser to confirm.

**Verified structurally only, same caveat as every pass before this one:** no browser was
reachable this session. Verification was the real `TestClient` against the real app (fake
`Engine`, same fixture pattern as `tests/test_web_routes.py`) with the rendered HTML inspected
directly for the sidebar/caret/scanline markup, the "current" link, and that partials still come
back bare -- plus the full suite. 763 tests still pass (no new ones needed -- the existing route
tests already assert nav/current/partial-is-bare behavior structurally, via `_nav_tag()`, not by
exact markup, so they kept working through the sidebar rewrite unchanged), `ruff`/`ruff
format`/`mypy` all clean. The visual result is still unverified until actually opened in a
browser.

## Stage A, fourth visual pass -- 2026-10-02: a real reference site, a real bug fix, and the first "looks good" since this started

The sidebar from the third pass had one real bug: the active nav link's "pressed button" effect
(shadow collapses, box translates into where it was) came straight from the old horizontal top
nav, where it read fine. In a vertical, full-width sidebar list it translated the active link
4px right/down, knocking it out of column alignment with every link above and below it -- caught
by the user as "doesn't look properly aligned when clicked." Fixed by dropping the translate
entirely in favor of an inset glow (`box-shadow: inset 0 0 0 1px ...`), which reads as "pressed"
without moving the box's edges at all.

**Then the user pointed at a concrete reference, https://tetsuobot.com, as "more or less the
look I'm aiming for."** Per this document's own rule, that got pulled apart for real rather than
designed from the name: `WebFetch`/`ctx_fetch_and_index` for the page structure (a terminal/CLI
aesthetic -- boot-sequence text, `tetsuobot:~$` prompts, ASCII progress bars -- not literally
8-bit pixel), then the actual live CSS fetched and regex-parsed for real hex colors, font-family
values, and border-radius/texture keywords, since a markdown conversion of the page would have
thrown all of that away. Found: `JetBrains Mono` + `Orbitron`, a near-black `#0a0a0f`/`#12121a`
background, a dominant cyan `#00f0ff` glow accent (used in dozens of `rgba(0,240,255,*)` shadow
opacities), a sparing magenta `#ff00aa`, small `2-4px` border-radius, and grid/noise/glitch
texture keywords.

**One open call before touching anything: Tetsuo's palette vs. this project's own validated
status colors.** Asked rather than assumed, since "never re-derive the status/ink colors" had
held across all three earlier passes. **Decided: keep the validated `good`/`warning`/`serious`/
`critical` hexes exactly as they are; adopt only Tetsuo's chrome** (background, ink, fonts,
cyan/magenta accent, grid texture). Concretely this meant *removing* the previous pass's
glow-on-`badge-good` touch, not keeping it -- a cyan glow on the "good" badge would have started
reading as "cyan means good," exactly the status/accent collision the skill's rule exists to
prevent, now doubly true with a second accent hue (magenta) in play that means nothing at all.
The decorative accent only ever touches structural chrome now: nav hover/active glow, panel
edges, the title caret.

**Fonts vendored the same way as before** (fetched once from `fonts.gstatic.com` with a
browser user-agent, not CDN-loaded) -- `jetbrains-mono.woff2` turned out to be a single variable
file that Google's own CSS references twice, once per weight declaration (400 and 700 pointing
at the identical URL); replicated that exactly rather than fetching "two" files that are actually
one. `orbitron-700.woff2` is the one weight used. Old `press-start-2p.woff2`/`vt323.woff2`
deleted rather than left as dead weight in `copier.spec`'s directory-level `static/` `datas`
entry.

**Deliberately left out: the reference site's animated glitch-flicker on its hero title.** That
reads fine as a one-off landing-page hero line; this title sits in the sidebar of every page of a
live trading dashboard, permanently in view, and an unreviewable constant flicker risked
annoyance over flair. Kept the existing static blinking cursor (recolored cyan) instead -- stated
as a scoping choice, not silently dropped.

**This is the first pass in the whole web-UI saga where opening it for real produced "looks much
better" instead of a bug report or "still lacking."** Still worth recording plainly: every prior
"verified structurally, unseen in a browser" caveat in this document was eventually followed by
either a real bug (the flashing fix) or a redesign ask (this whole chain) once actually opened --
this is the first time that loop closed clean. 763 tests pass, `ruff`/`ruff format`/`mypy` clean.

## What this plan deliberately does not cover

Hosting this for anyone not on the same machine, multi-user accounts/roles within the web UI,
and anything about the breakeven/prop-firm-guard trading logic itself (that belongs in
`ROADMAP.md`, once Web Stage D is reached). Out of scope until asked for.
