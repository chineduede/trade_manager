"""One JSON document on disk: written atomically, read with caution.

Every JSON file the app keeps -- the config, the guards, the virtual book, the
shared stores -- holds one document that is rewritten whole. How such a file is written, and
what happens when one cannot be read, used to be said in half a dozen places, each its own
copy of the same temp-file dance; this module says it once, as :mod:`mt5copier.sqlite_store`
does for the SQLite files.

:func:`write_json` is the one writer: a temp file beside the target, fsync, then
``os.replace``, so a crash never leaves half a file. Reading splits by what the file is
worth. :func:`read_json` is for files that are a cache or an overlay: unreadable is the same
as absent, said once in the log. A file that is the only copy of what the person built gets
:func:`quarantine` instead -- moved aside, never deleted, so the evidence survives for a
human while the app starts from the default.

Locking is the caller's: a file shared between windows is read-modify-written under
:func:`mt5copier.shared_data.shared_lock`, and nothing here needs to know that.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)


def write_json(target: Path, payload: object) -> None:
    """Write ``payload`` as indented JSON, atomically: a temp file beside the target, fsync,
    then ``os.replace``, which is atomic on NTFS. A half-written file after a crash would be
    worse than a stale one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
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


def read_json(path: Path, *, unreadable: str = "treated as absent") -> Any:
    """The parsed file, or None when it is absent or cannot be parsed. Never raises: the
    files read this way must all be optional, or a bad write in one window could stop every
    profile starting. ``unreadable`` finishes the log line, naming what the caller does
    without the file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("%s unreadable (%s); %s", path, exc, unreadable)
        return None


def quarantine(target: Path, reason: str, what: str) -> None:
    """Move a file that could not be read aside as ``.bad-<pid>.json`` -- never delete it:
    the evidence must survive for a human while the app starts from the default. ``reason``
    is already words, not the exception, so a caller whose file holds secrets can keep them
    out of the log; ``what`` names the file for it."""
    backup = target.with_suffix(f".bad-{os.getpid()}.json")
    try:
        target.replace(backup)
        log.error("%s at %s could not be read (%s); moved to %s", what, target, reason, backup)
    except OSError:
        log.error(
            "%s at %s could not be read (%s) and could not be moved aside", what, target, reason
        )
