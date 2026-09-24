"""Config schema, merge and persistence tests.

The merge behaviour is the part worth guarding hardest: a rescan must never silently discard
a role or a risk setting the user set, because that would quietly change what gets traded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mt5copier.config import (
    SCHEMA_VERSION,
    AppConfig,
    NoStopPolicy,
    Role,
    SizingMode,
    TerminalConfig,
    load,
    merge_discovery,
    save,
)
from mt5copier.discovery import DiscoveredTerminal

TID_A = "A" * 32
TID_B = "B" * 32


def disc(tid: str, label: str, exe: Path | None, **kw: object) -> DiscoveredTerminal:
    return DiscoveredTerminal(
        terminal_id=tid,
        data_path=None,
        install_path=exe.parent if exe else None,
        exe_path=exe,
        label=label,
        **kw,  # type: ignore[arg-type]
    )


# --- schema --------------------------------------------------------------------------------


def test_defaults_are_safe() -> None:
    """Copying must default to off: Stage 1 ships with no trading logic at all."""
    cfg = AppConfig()
    assert cfg.copying_enabled is False
    assert cfg.terminals == {}
    assert cfg.master is None


def test_rejects_two_masters() -> None:
    with pytest.raises(ValidationError, match="only one terminal may be master"):
        AppConfig(
            terminals={
                TID_A: TerminalConfig(terminal_id=TID_A, role=Role.MASTER),
                TID_B: TerminalConfig(terminal_id=TID_B, role=Role.MASTER),
            }
        )


def test_rejects_key_mismatch() -> None:
    with pytest.raises(ValidationError, match="!= terminal_id"):
        AppConfig(terminals={TID_A: TerminalConfig(terminal_id=TID_B)})


def test_rejects_unknown_fields() -> None:
    """extra='forbid' turns a typo in a hand-edited config into an error, not a silent no-op."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"terminals": {}, "muliplier_typo": 3})


def test_set_role_demotes_previous_master() -> None:
    cfg = AppConfig(
        terminals={
            TID_A: TerminalConfig(terminal_id=TID_A, role=Role.MASTER),
            TID_B: TerminalConfig(terminal_id=TID_B, role=Role.SLAVE),
        }
    )
    cfg.set_role(TID_B, Role.MASTER)
    assert cfg.terminals[TID_A].role is Role.UNASSIGNED
    assert cfg.master is not None and cfg.master.terminal_id == TID_B


def test_assigned_excludes_unassigned_and_disabled() -> None:
    cfg = AppConfig(
        terminals={
            TID_A: TerminalConfig(terminal_id=TID_A, role=Role.MASTER),
            TID_B: TerminalConfig(terminal_id=TID_B, role=Role.SLAVE, enabled=False),
        }
    )
    assert [t.terminal_id for t in cfg.assigned()] == [TID_A]


def test_sizing_defaults_to_percent_of_balance() -> None:
    cfg = TerminalConfig(terminal_id=TID_A)
    assert cfg.sizing_mode is SizingMode.PERCENT_OF_BALANCE
    assert cfg.risk_percent == 1.0


def test_only_two_sizing_modes_exist() -> None:
    """Sizing is risk-based only -- no lot multiplier, no fixed lot. A slave must never
    inherit the master's position size."""
    assert set(SizingMode) == {SizingMode.PERCENT_OF_BALANCE, SizingMode.FIXED_CASH_RISK}


@pytest.mark.parametrize(
    ("field", "value"), [("risk_percent", 0), ("risk_percent", 101), ("risk_cash", 0)]
)
def test_risk_bounds_enforced(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        TerminalConfig(terminal_id=TID_A, **{field: value})


def test_no_stop_defaults_to_skip() -> None:
    """Both sizing modes need a stop distance, so a stopless master trade cannot be sized.
    Skipping is the safe default -- better uncopied than opened at a guessed size."""
    assert TerminalConfig(terminal_id=TID_A).no_stop_policy is NoStopPolicy.SKIP


def test_lot_multiple_policy_defaults_to_mirroring_the_master_exactly() -> None:
    cfg = TerminalConfig(terminal_id=TID_A, no_stop_policy=NoStopPolicy.LOT_MULTIPLE)
    assert cfg.no_stop_lot_multiple == 1.0


def test_a_lot_multiple_must_be_positive() -> None:
    """Zero would silently mean "copy nothing" rather than "copy small"."""
    for bad in (0.0, -0.5):
        with pytest.raises(ValidationError):
            TerminalConfig(terminal_id=TID_A, no_stop_lot_multiple=bad)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"role": Role.SLAVE, "risk_percent": 1.5}, "1.5%"),
        (
            {"role": Role.SLAVE, "sizing_mode": SizingMode.FIXED_CASH_RISK, "risk_cash": 250},
            "250 cash",
        ),
        ({"role": Role.MASTER}, "-"),
        ({"role": Role.UNASSIGNED}, "-"),
    ],
)
def test_risk_label(kwargs: dict[str, object], expected: str) -> None:
    assert TerminalConfig(terminal_id=TID_A, **kwargs).risk_label() == expected  # type: ignore[arg-type]


# --- merge ---------------------------------------------------------------------------------


def test_merge_adds_new_terminals(tmp_path: Path) -> None:
    exe = tmp_path / "B" / "terminal64.exe"
    cfg = merge_discovery(AppConfig(), [disc(TID_A, "Broker A", exe, login=7, server="A-Live")])
    t = cfg.terminals[TID_A]
    assert (t.label, t.exe_path, t.last_login, t.last_server) == ("Broker A", exe, 7, "A-Live")
    assert t.role is Role.UNASSIGNED


def test_merge_preserves_user_settings(tmp_path: Path) -> None:
    """The whole point: a rescan refreshes discovery facts and touches nothing else."""
    exe = tmp_path / "B" / "terminal64.exe"
    cfg = AppConfig(
        terminals={
            TID_A: TerminalConfig(
                terminal_id=TID_A,
                label="stale label",
                role=Role.SLAVE,
                risk_cash=250.0,
                sizing_mode=SizingMode.FIXED_CASH_RISK,
                invert=True,
                symbol_blacklist=["XAUUSD"],
                symbol_suffix="p",
                symbol_map={"XAUUSD": "XAUUSD.s"},
            )
        }
    )
    merge_discovery(cfg, [disc(TID_A, "Broker A", exe, login=42, server="A-Live")])
    t = cfg.terminals[TID_A]
    assert t.label == "Broker A"  # refreshed
    assert t.exe_path == exe  # refreshed
    assert t.role is Role.SLAVE  # preserved
    assert t.risk_cash == 250.0  # preserved
    assert t.sizing_mode is SizingMode.FIXED_CASH_RISK
    assert t.invert is True
    assert t.symbol_blacklist == ["XAUUSD"]
    assert t.symbol_suffix == "p"
    assert t.symbol_map == {"XAUUSD": "XAUUSD.s"}


def test_merge_keeps_vanished_terminals(tmp_path: Path) -> None:
    """A terminal mid-upgrade must not lose its config just for being briefly absent."""
    cfg = AppConfig(
        terminals={TID_A: TerminalConfig(terminal_id=TID_A, role=Role.SLAVE, risk_percent=2.0)}
    )
    merge_discovery(cfg, [])
    assert cfg.terminals[TID_A].risk_percent == 2.0


def test_merge_does_not_null_exe_when_missing(tmp_path: Path) -> None:
    """If discovery cannot see the exe this pass, keep the last known good path."""
    exe = tmp_path / "B" / "terminal64.exe"
    cfg = merge_discovery(AppConfig(), [disc(TID_A, "Broker A", exe)])
    merge_discovery(cfg, [disc(TID_A, "Broker A", None)])
    assert cfg.terminals[TID_A].exe_path == exe


def test_merge_tracks_moved_install_path(tmp_path: Path) -> None:
    """Config is keyed on the instance hash precisely so a moved install is an update."""
    old = tmp_path / "old" / "terminal64.exe"
    new = tmp_path / "new" / "terminal64.exe"
    cfg = merge_discovery(AppConfig(), [disc(TID_A, "Broker A", old)])
    cfg.set_role(TID_A, Role.MASTER)
    merge_discovery(cfg, [disc(TID_A, "Broker A", new)])
    assert cfg.terminals[TID_A].exe_path == new
    assert cfg.terminals[TID_A].role is Role.MASTER


# --- persistence ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = AppConfig(poll_interval_ms=500)
    cfg.terminals[TID_A] = TerminalConfig(
        terminal_id=TID_A, label="Broker A", role=Role.MASTER, exe_path=tmp_path / "t.exe"
    )
    save(cfg, path)
    loaded = load(path)
    assert loaded.poll_interval_ms == 500
    assert loaded.master is not None and loaded.master.label == "Broker A"
    assert loaded.terminals[TID_A].exe_path == tmp_path / "t.exe"


def test_save_is_atomic_leaving_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save(AppConfig(), path)
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    assert load(tmp_path / "absent.json").terminals == {}


def test_load_corrupt_file_moves_it_aside_and_recovers(tmp_path: Path) -> None:
    """A bad config must never prevent startup, and must never be silently destroyed."""
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    cfg = load(path)
    assert cfg.terminals == {}
    assert not path.exists()
    assert any(p.name.startswith("config.bad-") for p in tmp_path.iterdir())


def test_load_schema_violation_also_recovers(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"poll_interval_ms": "banana"}), encoding="utf-8")
    assert load(path).poll_interval_ms == 250


def test_a_retired_field_is_dropped_rather_than_failing_the_whole_config(tmp_path: Path) -> None:
    """``extra="forbid"`` plus recover-by-defaults means a removed key would silently cost the
    user every setting they had. Strip it instead."""
    path = tmp_path / "config.json"
    tid = "A" * 32
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "terminals": {
                    tid: {
                        "terminal_id": tid,
                        "label": "Alpha",
                        "role": "slave",
                        "risk_percent": 2.5,
                        "default_sl_points": 200,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load(path)
    assert path.exists(), "a migratable config must not be quarantined"
    assert cfg.terminals[tid].risk_percent == 2.5
    assert cfg.terminals[tid].role is Role.SLAVE
    assert not hasattr(cfg.terminals[tid], "default_sl_points")
    assert cfg.version == SCHEMA_VERSION


def test_migration_tolerates_a_malformed_document(tmp_path: Path) -> None:
    """It runs before validation on anything the file happens to contain, so it must not be
    the thing that raises."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"terminals": ["not", "a", "dict"]}), encoding="utf-8")
    assert load(path).terminals == {}


def test_saved_json_is_human_readable(tmp_path: Path) -> None:
    """Indented, so hand-editing the file outside the UI is realistic."""
    path = tmp_path / "config.json"
    save(AppConfig(), path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n  ") and text.endswith("\n")


# --- symbol mapping -------------------------------------------------------------------------


def test_a_config_written_before_symbol_map_existed_still_loads(tmp_path: Path) -> None:
    """``extra="forbid"`` rejects unknown keys, never absent ones -- so an additive field with
    a default needs no migration. Getting this wrong costs the user every terminal's settings,
    because load() treats a validation failure as corruption."""
    path = tmp_path / "config.json"
    tid = "A" * 32
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "terminals": {
                    tid: {
                        "terminal_id": tid,
                        "label": "Alpha",
                        "role": "slave",
                        "risk_percent": 2.5,
                        "symbol_suffix": "p",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load(path)
    assert path.exists(), "a loadable config must not be quarantined"
    assert cfg.terminals[tid].symbol_map == {}
    assert cfg.terminals[tid].symbol_suffix == "p", "and nothing else is disturbed"
    assert cfg.terminals[tid].risk_percent == 2.5


def test_symbol_map_keys_are_normalised_to_upper_case() -> None:
    """Folded once here rather than at each of the three places that resolve a symbol -- a
    fold repeated three times is one that eventually disagrees with itself."""
    t = TerminalConfig(terminal_id="A" * 32, symbol_map={" eurgbp ": "EURGBPp"})
    assert t.symbol_map == {"EURGBP": "EURGBPp"}


def test_symbol_map_values_keep_the_brokers_own_casing() -> None:
    t = TerminalConfig(terminal_id="A" * 32, symbol_map={"EURGBP": "EURGBPp"})
    assert t.symbol_map["EURGBP"] == "EURGBPp"


def test_two_mapping_keys_differing_only_in_case_are_refused() -> None:
    """Choosing between them would be a guess about which instrument the user meant, which is
    the one thing symbol resolution must never do. Only reachable by hand-editing the file
    into a contradiction; the editor rejects it at entry."""
    with pytest.raises(ValidationError, match="disagree"):
        TerminalConfig(terminal_id="A" * 32, symbol_map={"eurgbp": "EURGBPp", "EURGBP": "EURGBP.r"})


def test_a_mapping_repeated_in_two_cases_but_agreeing_is_fine() -> None:
    t = TerminalConfig(terminal_id="A" * 32, symbol_map={"eurgbp": "EURGBPp", "EURGBP": "EURGBPp"})
    assert t.symbol_map == {"EURGBP": "EURGBPp"}


def test_symbol_map_is_saved_beside_the_other_symbol_fields(tmp_path: Path) -> None:
    """save() writes in field-declaration order, so this pins where it lands in the file."""
    path = tmp_path / "config.json"
    cfg = AppConfig(
        terminals={"A" * 32: TerminalConfig(terminal_id="A" * 32, symbol_map={"EURGBP": "EURGBPp"})}
    )
    save(cfg, path)
    keys = list(json.loads(path.read_text(encoding="utf-8"))["terminals"]["A" * 32])
    assert keys.index("symbol_suffix") < keys.index("symbol_map") < keys.index("symbol_whitelist")
