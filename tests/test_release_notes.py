"""The release notes: well-formed, covering the running version, and announced once.

The shape tests are what makes the release gate in ``release.ps1`` trustworthy: the gate
only compares the top entry's version string, so everything else about the tuple -- order,
dates, non-empty words -- is held here, where every commit's pytest run sees it.
"""

from __future__ import annotations

import json
import re

from mt5copier import __version__, release_notes
from mt5copier.desktop.api import DesktopApi
from mt5copier.paths import version_seen_path


def _nums(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


class TestTheNotes:
    def test_every_entry_is_well_formed(self) -> None:
        assert release_notes.NOTES
        for version, date, lines in release_notes.NOTES:
            assert re.fullmatch(r"\d+\.\d+\.\d+", version)
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
            assert lines
            assert all(line.strip() for line in lines)

    def test_newest_first_without_repeats(self) -> None:
        versions = [_nums(v) for v, _, _ in release_notes.NOTES]
        assert versions == sorted(versions, reverse=True)
        assert len(set(versions)) == len(versions)

    def test_the_running_version_has_notes(self) -> None:
        # The gate demands the *top* entry at a release; between releases the version just
        # cut must still be present, or What's new would say nothing about the build it is in.
        assert __version__ in {v for v, _, _ in release_notes.NOTES}


class TestSeenOnce:
    def test_a_fresh_install_announces_nothing_and_primes_the_file(self) -> None:
        assert not version_seen_path().exists()
        assert release_notes.unseen() is False
        primed = json.loads(version_seen_path().read_text(encoding="utf-8"))
        assert primed == {"last": __version__}

    def test_an_update_is_unseen_until_marked(self) -> None:
        release_notes.mark_seen()
        version_seen_path().write_text(json.dumps({"last": "0.0.1"}), encoding="utf-8")
        assert release_notes.unseen() is True
        # Asking is not reading: only the dismissal marks, so a launch that dies with the
        # modal open shows it again next time.
        assert release_notes.unseen() is True
        release_notes.mark_seen()
        assert release_notes.unseen() is False

    def test_a_broken_file_reprimes_quietly(self) -> None:
        release_notes.mark_seen()
        version_seen_path().write_text("not json", encoding="utf-8")
        assert release_notes.unseen() is False
        primed = json.loads(version_seen_path().read_text(encoding="utf-8"))
        assert primed == {"last": __version__}


class TestTheBridge:
    def test_version_state_carries_the_notes_and_notes_seen_settles_them(self) -> None:
        api = DesktopApi(cfg=None, engine=None)  # type: ignore[arg-type]
        version_seen_path().parent.mkdir(parents=True, exist_ok=True)
        version_seen_path().write_text(json.dumps({"last": "0.0.1"}), encoding="utf-8")
        state = api.version_state()
        assert state["version"] == __version__
        assert state["unseen"] is True
        assert state["notes"][0]["version"] == release_notes.NOTES[0][0]
        assert state["notes"][0]["lines"]
        assert api.notes_seen() == {"ok": True}
        assert api.version_state()["unseen"] is False
