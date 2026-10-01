"""FastAPI application factory for Stage A: a read-only dashboard over the Engine's state."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..engine import Engine
from .auth import TokenAuth
from .routes import build_router

_HERE = Path(__file__).resolve().parent


def create_app(engine: Engine, token: str) -> FastAPI:
    """Build the FastAPI app for ``mt5copier --web``.

    The engine is started on lifespan startup and stopped on shutdown -- the same two calls
    ``CopierApp.on_mount``/``on_unmount`` already make for the Textual UI (``tui/app.py``), just
    behind FastAPI's lifespan instead of Textual's mount hooks. One ``Engine``, one process,
    regardless of which UI is driving it (see WEBUI_PLAN.md's "One Engine, one process").
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        engine.start()
        try:
            yield
        finally:
            engine.stop()

    app = FastAPI(lifespan=lifespan, title="mt5copier", docs_url=None, redoc_url=None)
    auth = TokenAuth(token)
    app.include_router(build_router(engine, auth))
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    return app
