"""What the non-UI tests share: the three terminals they act on.

This is the surviving, UI-free half of the old ``tui_support.py``; the stub engine and the
Textual app wiring went with the TUI.
"""

from __future__ import annotations

from pathlib import Path

from mt5copier.config import AppConfig, TerminalConfig

TID_MASTER = "A" * 32
TID_SLAVE = "B" * 32
TID_BROKEN = "C" * 32


def make_cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        terminals={
            TID_MASTER: TerminalConfig(
                terminal_id=TID_MASTER, label="Alpha Broker", exe_path=exe, last_login=111
            ),
            TID_SLAVE: TerminalConfig(
                terminal_id=TID_SLAVE, label="Beta Broker", exe_path=exe, last_login=222
            ),
            # No exe_path: an uninstalled leftover, like the EXNESS install on this machine.
            TID_BROKEN: TerminalConfig(
                terminal_id=TID_BROKEN, label="Gone Broker", exe_path=None, last_login=333
            ),
        }
    )
