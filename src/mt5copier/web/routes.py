"""Stage A routes: a read-only dashboard page and the websocket that pushes it live.

No POST route exists anywhere in this module -- Stage A cannot touch a position, so there is no
write path yet for the custom-header CSRF defense described in WEBUI_PLAN.md to protect; that
starts in Stage B, with the first POST.

Push model: htmx's websocket extension swaps server-rendered HTML fragments in by id
(``hx-swap-oob``), not a hand-rolled JS client parsing a JSON API. Each tick considers the
health bar, one fragment per terminal row, and any new log lines -- matching the Textual TUI's
own ``_on_timer`` cadence (0.5s).

**Only a fragment that actually changed since this connection's last tick is sent.** A fragment
is never re-sent unchanged. An ``hx-swap-oob="true"`` swap replaces the whole element (a fresh
DOM node), so sending the health bar and every row unconditionally on every tick -- the first
version of this did exactly that -- means the browser is handed a brand new node for everything,
twice a second, forever, whether or not anything happened. That is not just wasted bandwidth: the
dashboard's own flash-on-update affordance (``dashboard.js``) fires on *any* added node with an
id, so the symptom was the whole page flashing continuously rather than only on a real change.
This is the same discipline ``tui/app.py``'s ``_rebuild_rows`` already follows for the Textual
UI ("only rebuild rows when the set of terminals changes, else update in place") -- ported here
as "only send a fragment whose rendered HTML actually differs from what this connection last
sent."
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..engine import Engine
from ..paths import log_dir
from .auth import TokenAuth
from .presentation import RowInfo, all_rows, read_log_tail, read_new_log_lines, render_log_lines

log = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
_TEMPLATES.env.filters["log_lines"] = render_log_lines

PUSH_INTERVAL_S = 0.5
"""Matches the Textual TUI's own ``_on_timer`` cadence -- no reason to push the browser faster
than the UI this one is modeled on."""

LOG_TAIL_LINES = 200


def _log_path() -> Path:
    return log_dir() / "copier.log"


def _render_health(engine: Engine) -> str:
    template = _TEMPLATES.get_template("_health.html")
    return template.render(health=engine.health(), blocked_reason=engine.blocked_reason)


def _render_row(row: RowInfo) -> str:
    return _TEMPLATES.get_template("_row.html").render(row=row)


def _render_log_append(text: str) -> str:
    return _TEMPLATES.get_template("_log_append.html").render(text=text)


def build_router(engine: Engine, auth: TokenAuth) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request, _: None = Depends(auth.require)) -> HTMLResponse:
        tail = read_log_tail(_log_path(), max_lines=LOG_TAIL_LINES)
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "token": request.query_params.get("token", ""),
                "rows": all_rows(engine),
                "health": engine.health(),
                "blocked_reason": engine.blocked_reason,
                "log_text": tail.text,
                "log_offset": tail.offset,
            },
        )

    @router.websocket("/ws/state")
    async def ws_state(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token")
        if not auth.check(token):
            # Rejected before accept(): uvicorn answers the handshake itself (as an HTTP 403),
            # so a browser with the wrong token never gets a connected socket at all.
            await websocket.close(code=1008)
            return
        await websocket.accept()
        offset_param = websocket.query_params.get("log_offset")
        log_offset = int(offset_param) if offset_param and offset_param.isdigit() else 0

        # Seeded from the engine's current state, not empty: the index page this connection
        # follows already rendered this same state moments ago, so the first tick must not
        # re-send it all as if it were new.
        last_health = _render_health(engine)
        last_rows = {row.terminal_id: _render_row(row) for row in all_rows(engine)}
        try:
            while True:
                await asyncio.sleep(PUSH_INTERVAL_S)

                health_html = _render_health(engine)
                if health_html != last_health:
                    await websocket.send_text(health_html)
                    last_health = health_html

                for row in all_rows(engine):
                    row_html = _render_row(row)
                    if last_rows.get(row.terminal_id) != row_html:
                        await websocket.send_text(row_html)
                        last_rows[row.terminal_id] = row_html

                tail = read_new_log_lines(_log_path(), log_offset)
                log_offset = tail.offset
                if tail.text:
                    await websocket.send_text(_render_log_append(tail.text))
        except WebSocketDisconnect:
            log.debug("dashboard client disconnected")

    return router
