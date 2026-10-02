"""Stage A routes: three read-only pages, each live-updated by htmx polling its own partial
route -- no websocket, no JSON API.

No POST route exists anywhere in this module -- Stage A cannot touch a position, so there is no
write path yet for the custom-header CSRF defense described in WEBUI_PLAN.md to protect; that
starts in Stage B, with the first POST.

Every page's live panel is a stable element carrying ``hx-get`` + ``hx-trigger="every Ns"`` +
``hx-swap="innerHTML"``. htmx establishes the polling interval once, when that element is first
processed, and keeps firing it for as long as the element stays in the DOM -- which it does,
forever, since ``innerHTML`` replaces only its *children* on each poll, never the element
itself. There is deliberately no self-replacing "outerHTML swap of self" trick here: that
pattern exists in htmx's own docs mainly so a response can *stop* the polling by omitting the
trigger from what it swaps back in, which this page never needs to do, and it would have broken
the log panel's auto-scroll (``dashboard.js``'s ``MutationObserver`` watches one specific node;
replacing that node outright would leave the observer watching a detached element).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..engine import Engine
from ..paths import log_dir
from .auth import TokenAuth
from .presentation import all_rows, badge_for_tier, read_log_tail, render_log_lines

_TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
_TEMPLATES.env.filters["log_lines"] = render_log_lines
_TEMPLATES.env.filters["tier_badge"] = badge_for_tier

LOG_TAIL_LINES = 200


@dataclass(frozen=True)
class NavItem:
    """One entry in the sidebar. Adding a page is adding one of these plus one route --
    `base.html` renders the list it's given, it never hardcodes a link itself."""

    slug: str
    label: str
    path: str


# The single place that knows what pages exist. Stage B/C add SETTINGS / ROLES / OVERRIDE here
# and nothing in base.html has to change for the link to appear.
NAV: list[NavItem] = [
    NavItem("home", "HOME", "/"),
    NavItem("terminals", "TERMINALS", "/terminals"),
    NavItem("log", "LOG", "/log"),
]


def _log_path() -> Path:
    return log_dir() / "copier.log"


def build_router(engine: Engine, auth: TokenAuth) -> APIRouter:
    router = APIRouter()

    def _token(request: Request) -> str:
        return request.query_params.get("token", "")

    def _page(request: Request, template: str, page: str, **extra: Any) -> HTMLResponse:
        """Shared context every full page needs (token + the nav list to render) so a route
        body only ever supplies what's actually page-specific."""
        return _TEMPLATES.TemplateResponse(
            request,
            template,
            {"token": _token(request), "page": page, "nav": NAV, **extra},
        )

    # --- pages -------------------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    def home(request: Request, _: None = Depends(auth.require)) -> HTMLResponse:
        return _page(
            request,
            "index.html",
            "home",
            health=engine.health(),
            blocked_reason=engine.blocked_reason,
        )

    @router.get("/terminals", response_class=HTMLResponse)
    def terminals_page(request: Request, _: None = Depends(auth.require)) -> HTMLResponse:
        return _page(request, "terminals.html", "terminals", rows=all_rows(engine))

    @router.get("/log", response_class=HTMLResponse)
    def log_page(request: Request, _: None = Depends(auth.require)) -> HTMLResponse:
        return _page(
            request,
            "log.html",
            "log",
            log_text=read_log_tail(_log_path(), max_lines=LOG_TAIL_LINES),
        )

    # --- partials, polled by the pages above ------------------------------------------------

    @router.get("/partials/health", response_class=HTMLResponse)
    def partial_health(request: Request, _: None = Depends(auth.require)) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request,
            "_health_inner.html",
            {"health": engine.health(), "blocked_reason": engine.blocked_reason},
        )

    @router.get("/partials/terminals", response_class=HTMLResponse)
    def partial_terminals(request: Request, _: None = Depends(auth.require)) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request, "_terminals_inner.html", {"rows": all_rows(engine)}
        )

    @router.get("/partials/log", response_class=HTMLResponse)
    def partial_log(request: Request, _: None = Depends(auth.require)) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request,
            "_log_inner.html",
            {"log_text": read_log_tail(_log_path(), max_lines=LOG_TAIL_LINES)},
        )

    return router
