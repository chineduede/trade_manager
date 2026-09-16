"""Manual smoke check: run passive discovery and print what it found.

Deliberately connects to nothing, so this is safe to run at any time -- it will not launch a
single MT5 terminal. Usage:  uv run python scripts/probe_discovery.py
"""

from __future__ import annotations

from mt5copier.discovery import discover_terminals

HEADER = f"{'RUN':<4} {'LOGIN':>11}  {'SERVER':<22} {'LABEL':<26} NOTES"


def main() -> None:
    terminals = discover_terminals()
    print(f"\n{len(terminals)} terminals discovered\n")
    print(HEADER)
    print("-" * len(HEADER))
    for t in terminals:
        run = "live" if t.is_running else ""
        login = str(t.login) if t.login is not None else "-"
        notes = "; ".join(t.notes)
        if not t.usable:
            notes = f"UNUSABLE {notes}".strip()
        print(f"{run:<4} {login:>11}  {(t.server or '-'):<22} {t.label:<26} {notes}")

    print(
        f"\nrunning={sum(1 for t in terminals if t.is_running)}  "
        f"usable={sum(1 for t in terminals if t.usable)}"
    )

    multi = [t for t in terminals if len(t.servers_seen) > 1]
    if multi:
        print("\nterminals that have seen more than one server:")
        for t in multi:
            print(f"  {t.label:<26} {', '.join(t.servers_seen)}")


if __name__ == "__main__":
    main()
