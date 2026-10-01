"""Entry point for ``mt5copier --web``.

Same shape as ``tui.app.run()``: load config, fold in a discovery sweep, then hand over --
here, to uvicorn instead of Textual. Still only one ``Engine``, so this acquires the same
``SingleInstanceLock`` as a TUI run (wired in ``__main__.py``), not a second kind of instance.
"""

from __future__ import annotations

import logging
import secrets

import uvicorn

from .. import config as config_mod
from ..discovery import discover_terminals
from ..engine import Engine
from ..paths import config_path
from .app import create_app

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765


def run(*, port: int = DEFAULT_PORT) -> None:
    cfg = config_mod.load()
    try:
        config_mod.merge_discovery(cfg, discover_terminals())
        config_mod.save(cfg)
    except Exception:
        log.exception("initial discovery failed; starting with saved config only")
    log.info("config at %s", config_path())

    token = secrets.token_urlsafe(32)
    engine = Engine(cfg)
    app = create_app(engine, token)

    url = f"http://127.0.0.1:{port}/?token={token}"
    print(f"mt5copier web UI: {url}")
    log.info("web UI starting on 127.0.0.1:%d", port)

    # 127.0.0.1 is written directly into this call, not read from a config field or CLI flag --
    # WEBUI_PLAN.md's non-negotiable is "no path to 0.0.0.0 without a separate, deliberately
    # awkward flag," and Stage A does not add that flag at all.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
