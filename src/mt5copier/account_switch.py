"""Which accounts a terminal has been logged into, and logging it into one of them.

**Only an unassigned terminal is switched.** A terminal with a role holds copies made on the
account it is logged into, filed by terminal; switching under them would leave records of
tickets that are not on the new account. Unassigning first already asks about those copies and
lets them go, so switching itself never has to.

**The account list is read from disk, without connecting.** A terminal keeps a folder for each
account that has ever connected in it, ``bases/<server>/trades/<login>``. That says the account
was used there, not that its password is still saved -- a login to one whose password is not
saved fails, and says so.

**No password is ever handled.** ``login()`` is called without one, and MT5 then uses the one
saved in the terminal for that account. Discovery's rule stands: no credentials anywhere.

The login itself runs in a process of its own, for the reason every worker does: the
``MetaTrader5`` package holds one connection per process, and this one must not share it.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logging_setup import configure, get_logger
from .paths import mt5_terminal_root

if TYPE_CHECKING:
    from multiprocessing.queues import Queue

log = get_logger(__name__)

_LOGIN_TIMEOUT_MS = 60_000
"""How long ``login()`` may take, MT5's own default."""
_WAIT_S = 120.0
"""How long to wait for the login process as a whole: starting the terminal, then logging in."""
_LOGIN_DIR_RE = re.compile(r"^\d+$")


@dataclass(frozen=True, slots=True)
class Account:
    login: int
    server: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    ok: bool
    said: str
    """What happened, in words a person can act on."""
    login: int | None = None
    server: str | None = None
    """The account the terminal reports afterwards, when it could be read."""


def known_accounts(terminal_id: str, root: Path | None = None) -> list[Account]:
    """Every account that has connected in this terminal, by server then login. Empty for a
    terminal whose data folder is not under the MetaQuotes root (a portable install)."""
    bases = (root or mt5_terminal_root()) / terminal_id / "bases"
    found: list[Account] = []
    try:
        servers = sorted(d for d in bases.iterdir() if d.is_dir())
    except OSError:
        return []
    for server in servers:
        try:
            logins = [
                int(d.name)
                for d in (server / "trades").iterdir()
                if d.is_dir() and _LOGIN_DIR_RE.match(d.name)
            ]
        except OSError:
            continue
        found += [Account(login, server.name) for login in sorted(logins) if login > 0]
    return found


def log_in(exe_path: Path, account: Account, log_file: Path) -> LoginResult:
    """Log the terminal at ``exe_path`` into ``account``, starting it if it is not running.
    Blocks until it is done or has taken too long; run it off the UI thread."""
    ctx = mp.get_context("spawn")
    out_q: Queue[LoginResult] = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=run_login,
        name="mt5-login",
        args=(str(exe_path), account.login, account.server, out_q, str(log_file)),
        daemon=True,
    )
    process.start()
    try:
        return out_q.get(timeout=_WAIT_S)
    except queue.Empty:
        return LoginResult(False, f"no answer from the terminal in {_WAIT_S:.0f}s")
    finally:
        process.join(timeout=5)
        if process.is_alive():
            process.kill()


def run_login(
    exe_path: str,
    login: int,
    server: str,
    out_q: Queue[LoginResult],
    log_file: str,
    _mt5: Any = None,
) -> None:
    """Process entry point: connect, log in, read back the account, disconnect.

    Module-level, as :func:`worker.run_worker` is, so the spawn start method can find it.
    The terminal is left running, logged into the account: it is about to be given a role, and
    its worker connects to it as it is. ``_mt5`` is a test seam.
    """
    configure(Path(log_file))
    if _mt5 is None:
        import MetaTrader5 as mt5  # noqa: N813 -- the package's own capitalisation

        _mt5 = mt5
    out_q.put(_login(_mt5, Path(exe_path), Account(login, server)))


def _login(mt5: Any, exe_path: Path, account: Account) -> LoginResult:
    from .worker import _Session

    session = _Session(mt5, exe_path)
    ok, error = session.connect()
    if not ok:
        return LoginResult(False, f"could not connect to the terminal: {error}")
    try:
        try:
            done = mt5.login(account.login, server=account.server, timeout=_LOGIN_TIMEOUT_MS)
        except Exception as exc:  # the extension can raise, not just return False
            return LoginResult(False, f"login raised: {exc!r}")
        if not done:
            code, words = _last_error(mt5)
            log.warning("login to %s@%s failed: %s %s", account.login, account.server, code, words)
            return LoginResult(
                False,
                f"login failed ({words or code}): is its password saved in the terminal? "
                "Log in once from MT5 with 'Save password' ticked",
            )
        info = mt5.account_info()
        login = getattr(info, "login", None)
        server = getattr(info, "server", None)
        if login != account.login:
            return LoginResult(
                False, f"the terminal reports account {login} after the login", login, server
            )
        log.info("logged in to %s@%s", login, server)
        return LoginResult(True, "logged in", login, server)
    finally:
        session.disconnect()


def _last_error(mt5: Any) -> tuple[object, str]:
    try:
        code, words = mt5.last_error()
    except Exception:
        return None, ""
    return code, str(words)
