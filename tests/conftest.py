"""Suite-wide isolation: no test may touch the user's real data.

``paths.user_data_dir()`` reads ``%APPDATA%`` at call time, and the config, the logs, the
dry-run store and -- the one that matters -- ``mapping.db`` all hang off it. Several tests build
an ``Engine`` without naming those paths, and a test that goes live opens the *real*
``mapping.db``: the copier's only record of which positions it owns. Nothing here can lose money
(the rows written are for terminals that do not exist), but a suite that writes to the live store
is one keystroke from wiping it, and it made ``test_a_result_answering_a_command_we_gave_up_on``
fail whenever two runs overlapped, because they were sharing a database.

So every test gets its own empty ``%APPDATA%``. Spawned worker processes inherit the environment,
so they are covered too.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_data(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path_factory.mktemp("appdata")))
    # And no inherited profile: a developer running the suite from a shell that launched a
    # profile would otherwise aim every test at that profile's layout.
    monkeypatch.delenv("MT5COPIER_PROFILE", raising=False)
