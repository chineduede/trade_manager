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

## What this plan deliberately does not cover

Hosting this for anyone not on the same machine, multi-user accounts/roles within the web UI,
and anything about the breakeven/prop-firm-guard trading logic itself (that belongs in
`ROADMAP.md`, once Web Stage D is reached). Out of scope until asked for.
