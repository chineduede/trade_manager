"""The pywebview desktop window: ``copier --desktop``.

The first milestone of the desktop app decided 2026-11-14: one window holding the
direct-trading page -- the ticket, a real candlestick chart with draggable levels, and the
book. The TUI stays the default and keeps every other page; this grows page by page only
if it proves out.

Boot mirrors :func:`mt5copier.tui.run` exactly -- same config, same guards, same engine --
because the window is a view, not a different app. The WebView2 profile lives under this
profile's own data directory, so side-by-side profiles stay isolated down to their
browser storage, matching the engine-level claim locks.
"""

from __future__ import annotations

from pathlib import Path

from .. import config as config_mod
from ..claims import ClaimBook
from ..ctrader import auth as ctrader_auth
from ..discovery import discover_terminals
from ..engine import Engine
from ..guards import model as guards_mod
from ..logging_setup import get_logger
from ..paths import (
    DEFAULT_PROFILE,
    active_profile,
    claims_dir,
    config_path,
    guards_path,
    user_data_dir,
)

log = get_logger(__name__)

_WEB = Path(__file__).resolve().parent / "web"


def run() -> None:
    """Entry point: load config, fold in a discovery sweep, then hand over to the window."""
    import webview  # heavy, and absent from TUI-only installs: imported only on --desktop

    from .api import DesktopApi

    profile = active_profile()
    cfg = config_mod.load()
    guards = guards_mod.load(guards_path())
    try:
        config_mod.merge_discovery(cfg, discover_terminals())
        config_mod.save(cfg)
    except Exception:
        log.exception("initial discovery failed; starting with saved config only")
    try:
        ctrader_auth.encrypt_saved()
        ctrader_auth.refresh_if_due()
    except Exception as exc:
        log.warning("could not refresh the cTrader login (%s)", exc)
    log.info("config at %s (profile %s, desktop)", config_path(), profile)

    engine = Engine(cfg, guards=guards, claims=ClaimBook(claims_dir(), profile))
    api = DesktopApi(cfg, engine)
    title = "Copier" if profile == DEFAULT_PROFILE else f"Copier — {profile}"
    window = webview.create_window(
        title,
        url=str(_WEB / "index.html"),
        js_api=api,
        width=1500,
        height=980,
        min_size=(1080, 720),
        frameless=True,
        easy_drag=False,
        background_color="#1b1813",
    )
    if window is None:  # pywebview types this as fallible; in practice it raises instead
        raise RuntimeError("pywebview could not create the window")
    api.attach(window)

    def _native_frame(*_args: object) -> None:
        """Once the window exists, give it back the styles Snap needs -- without the frame
        they would normally paint. See :mod:`.winframe`."""
        from . import winframe

        try:
            # ``native`` is the WinForms form; pywebview types it as None until shown.
            winframe.adopt(int(window.native.Handle.ToInt64()))  # type: ignore[attr-defined]
        except Exception:
            log.exception("could not adopt native frame behaviours; the window still runs")

    window.events.shown += _native_frame

    engine.start()
    try:
        # Blocks until the window closes. One window per process, one process per profile:
        # the profile's own WebView2 storage keeps instances apart, like everything else.
        webview.start(
            private_mode=False,
            storage_path=str(user_data_dir() / "webview"),
        )
    finally:
        engine.stop()


__all__ = ["run"]
