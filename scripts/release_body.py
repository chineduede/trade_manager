"""Write a GitHub Release body for one version, from the app's own changelog.

    uv run python scripts/release_body.py v0.1.9 body.md

The body is the matching ``release_notes.NOTES`` entry as markdown bullets -- the same
words the in-app What's new modal shows, so the Release page and the app never tell two
stories. It is written straight to the file as UTF-8, because a shell redirect's encoding
is whatever the console feels like that day. An unknown version is an error: the release
pipeline's notes gate means every shipped version has an entry, so a miss here is a wrong
tag, not a missing paragraph.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mt5copier.release_notes import NOTES


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    version = sys.argv[1].removeprefix("v")
    for noted, _date, lines in NOTES:
        if noted == version:
            body = f"**What's new in {version}**\n\n"
            body += "".join(f"- {line}\n" for line in lines)
            Path(sys.argv[2]).write_text(body, encoding="utf-8")
            return 0
    print(f"no release_notes.py entry for version {version}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
