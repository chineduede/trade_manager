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
    POLICY_FIELDS,
    SCHEMA_VERSION,
    VIRTUAL_MASTER_ID,
    AppConfig,
    AppMode,
    Management,
    NoStopPolicy,
    Role,
    SizingMode,
    TerminalConfig,
    account_key,
    load,
    merge_discovery,
    record_login,
    remember_policy,
    restore_policy,
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


def test_replace_terminal_swaps_in_the_whole_object() -> None:
    """A reader on another thread must see the old settings or the new, never half of each --
    which only holds if the terminal is replaced, not patched a field at a time."""
    cfg = AppConfig(terminals={TID_A: TerminalConfig(terminal_id=TID_A, role=Role.SLAVE)})
    old = cfg.terminals[TID_A]
    cfg.replace_terminal(old.model_copy(update={"risk_percent": 0.5}))
    assert cfg.terminals[TID_A] is not old
    assert cfg.terminals[TID_A].risk_percent == 0.5
    assert old.risk_percent == 1.0, "the object a reader already holds is left as it was"


def test_replace_terminal_does_not_move_the_terminal_in_the_dict() -> None:
    """Assigning to an existing key keeps the dict's size, so another thread iterating it
    (the engine walks it every tick) is not disturbed."""
    cfg = AppConfig(
        terminals={
            TID_A: TerminalConfig(terminal_id=TID_A),
            TID_B: TerminalConfig(terminal_id=TID_B),
        }
    )
    cfg.replace_terminal(cfg.terminals[TID_B].model_copy(update={"copy_pending": True}))
    assert list(cfg.terminals) == [TID_A, TID_B]


def test_replace_terminal_refuses_a_change_of_role() -> None:
    """set_role is the only place the one-master rule is kept, so it is the only way to move a
    role; a settings edit that carried one across would bypass it."""
    cfg = AppConfig(
        terminals={
            TID_A: TerminalConfig(terminal_id=TID_A, role=Role.MASTER),
            TID_B: TerminalConfig(terminal_id=TID_B, role=Role.SLAVE),
        }
    )
    with pytest.raises(ValueError, match="role"):
        cfg.replace_terminal(cfg.terminals[TID_B].model_copy(update={"role": Role.MASTER}))
    assert cfg.terminals[TID_A].role is Role.MASTER
    assert cfg.terminals[TID_B].role is Role.SLAVE


def test_replace_terminal_refuses_a_terminal_it_does_not_know() -> None:
    """Adding a terminal is discovery's job. A stray edit must not create one."""
    cfg = AppConfig(terminals={TID_A: TerminalConfig(terminal_id=TID_A)})
    with pytest.raises(KeyError):
        cfg.replace_terminal(TerminalConfig(terminal_id=TID_B))
    assert list(cfg.terminals) == [TID_A]


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


def test_account_key_is_none_with_no_login() -> None:
    assert account_key(None, "A-Live") is None


def test_account_key_combines_login_and_server() -> None:
    """Server is part of the key -- two different brokers can hand out the same login number."""
    assert account_key(123, "A-Live") != account_key(123, "B-Live")


def test_display_name_prefers_the_named_account_over_the_label() -> None:
    cfg = AppConfig(
        terminals={
            TID_A: TerminalConfig(
                terminal_id=TID_A, label="Alpha Broker", last_login=123, last_server="A-Live"
            )
        },
        account_names={account_key(123, "A-Live"): "My Funded Account"},
    )
    assert cfg.display_name(TID_A) == "My Funded Account"


def test_display_name_falls_back_to_label_then_to_the_id() -> None:
    cfg = AppConfig(terminals={TID_A: TerminalConfig(terminal_id=TID_A, label="Alpha Broker")})
    assert cfg.display_name(TID_A) == "Alpha Broker"
    cfg2 = AppConfig(terminals={TID_A: TerminalConfig(terminal_id=TID_A)})
    assert cfg2.display_name(TID_A) == TID_A[:8]


def test_display_name_ignores_an_account_name_for_a_different_account() -> None:
    """The name is keyed by account, not terminal -- a name set for some other login/server
    must never leak onto a terminal that isn't logged into that account."""
    cfg = AppConfig(
        terminals={
            TID_A: TerminalConfig(
                terminal_id=TID_A, label="Alpha Broker", last_login=123, last_server="A-Live"
            )
        },
        account_names={account_key(999, "A-Live"): "Someone Else's Account"},
    )
    assert cfg.display_name(TID_A) == "Alpha Broker"


def test_display_name_follows_the_account_to_a_different_terminal() -> None:
    """The whole point: the same account logged into a different terminal shows the same name."""
    key = account_key(123, "A-Live")
    cfg = AppConfig(
        terminals={
            TID_A: TerminalConfig(
                terminal_id=TID_A, label="Terminal One", last_login=123, last_server="A-Live"
            ),
            TID_B: TerminalConfig(
                terminal_id=TID_B, label="Terminal Two", last_login=123, last_server="A-Live"
            ),
        },
        account_names={key: "My Funded Account"},
    )
    assert cfg.display_name(TID_A) == "My Funded Account"
    assert cfg.display_name(TID_B) == "My Funded Account"


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
        },
        account_names={account_key(42, "A-Live"): "My Funded Account"},
    )
    merge_discovery(cfg, [disc(TID_A, "Broker A", exe, login=42, server="A-Live")])
    t = cfg.terminals[TID_A]
    assert t.label == "Broker A"  # refreshed
    assert t.exe_path == exe  # refreshed
    assert cfg.display_name(TID_A) == "My Funded Account"  # account name, untouched by the merge
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


def test_load_demotes_a_slave_found_with_no_master(tmp_path: Path) -> None:
    """A slave with no master makes no sense -- fixed on load, the same way a retired field is,
    rather than enforced as a model validator that would quarantine exactly the file this
    repairs (see ``_demote_orphaned_slaves``'s own docstring)."""
    path = tmp_path / "config.json"
    save(
        AppConfig(terminals={TID_A: TerminalConfig(terminal_id=TID_A, role=Role.SLAVE)}),
        path,
    )
    # Written directly as a slave with no master -- save() itself goes through pydantic, which
    # does not forbid this shape, only AppConfig._validate_roles's one-master rule does.

    cfg = load(path)
    assert cfg.terminals[TID_A].role is Role.UNASSIGNED
    # The fix is persisted immediately, not just corrected in memory.
    assert load(path).terminals[TID_A].role is Role.UNASSIGNED


def test_load_leaves_a_slave_alone_when_its_master_is_present(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save(
        AppConfig(
            terminals={
                TID_A: TerminalConfig(terminal_id=TID_A, role=Role.MASTER),
                TID_B: TerminalConfig(terminal_id=TID_B, role=Role.SLAVE),
            }
        ),
        path,
    )
    cfg = load(path)
    assert cfg.terminals[TID_B].role is Role.SLAVE


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
    master_tid = "B" * 32
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
                    },
                    # A master has to exist too, or load()'s no-master-no-slave cleanup would
                    # demote "Alpha" to unassigned before this test ever gets to the field it's
                    # actually about.
                    master_tid: {"terminal_id": master_tid, "label": "Master", "role": "master"},
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


def test_a_config_saved_with_custom_name_still_loads_with_every_setting(tmp_path: Path) -> None:
    """The regression that reset a real config on 2026-10-02: a development build saved
    ``custom_name`` on every terminal, the field was then removed, and the next load failed and
    started from defaults -- dry run back on, roles and risk settings gone."""
    path = tmp_path / "config.json"
    master_tid, slave_tid = "A" * 32, "B" * 32
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "dry_run": False,
                "copying_enabled": True,
                "terminals": {
                    master_tid: {
                        "terminal_id": master_tid,
                        "label": "Master",
                        "role": "master",
                        "custom_name": "",
                    },
                    slave_tid: {
                        "terminal_id": slave_tid,
                        "label": "Slave",
                        "role": "slave",
                        "risk_cash": 300.0,
                        "custom_name": "My Funded",
                        "last_login": 42,
                        "last_server": "Broker-Live",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.dry_run is False
    assert cfg.copying_enabled is True
    assert cfg.terminals[slave_tid].role is Role.SLAVE
    assert cfg.terminals[slave_tid].risk_cash == 300.0
    assert cfg.display_name(slave_tid) == "My Funded", "a name that was set is kept, by account"
    assert not list(tmp_path.glob("*.bad-*.json")), "nothing was treated as corrupt"


def test_a_pause_does_not_outlive_the_slave_role() -> None:
    """Unassigned while paused and made a slave again later, it starts out copying."""
    cfg = AppConfig(
        terminals={
            "A": TerminalConfig(terminal_id="A", role=Role.MASTER),
            "B": TerminalConfig(terminal_id="B", role=Role.SLAVE, paused=True),
        }
    )
    cfg.set_role("B", Role.UNASSIGNED)
    assert cfg.terminals["B"].paused is False


def test_setting_the_same_role_again_keeps_the_pause() -> None:
    cfg = AppConfig(
        terminals={
            "A": TerminalConfig(terminal_id="A", role=Role.MASTER),
            "B": TerminalConfig(terminal_id="B", role=Role.SLAVE, paused=True),
        }
    )
    cfg.set_role("B", Role.SLAVE)
    assert cfg.terminals["B"].paused is True


# --- who manages open trades ----------------------------------------------------------------


def test_copying_stops_and_partials_became_one_switch_taking_copy_sltps_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    master, a, b, c = "M" * 32, "A" * 32, "B" * 32, "C" * 32
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "terminals": {
                    master: {"terminal_id": master, "role": "master"},
                    a: {"terminal_id": a, "role": "slave", "copy_sltp": False},
                    b: {"terminal_id": b, "role": "slave", "copy_partials": False},
                    c: {"terminal_id": c, "role": "slave"},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load(path)
    assert [cfg.terminals[t].master_manages for t in (a, b, c)] == [False, True, True]
    assert cfg.terminals[master].management is Management.MASTER, "the master manages by default"


@pytest.mark.parametrize(
    ("management", "letting", "manages"),
    [
        (Management.MASTER, True, True),
        (Management.MASTER, False, False),
        (Management.SLAVES, True, False),
        (Management.SLAVES, False, False),
    ],
)
def test_the_master_manages_a_slaves_trades_only_with_both_gates_open(
    management: Management, letting: bool, manages: bool
) -> None:
    master = TerminalConfig(terminal_id="M" * 32, role=Role.MASTER, management=management)
    slave = TerminalConfig(terminal_id="S" * 32, role=Role.SLAVE, master_manages=letting)
    cfg = AppConfig(terminals={master.terminal_id: master, slave.terminal_id: slave})
    assert cfg.manages(slave) is manages


def test_with_no_master_nothing_is_managed_by_one() -> None:
    slave = TerminalConfig(terminal_id="S" * 32, role=Role.SLAVE)
    assert AppConfig(terminals={slave.terminal_id: slave}).manages(slave) is False


# --- direct trading mode ---------------------------------------------------------------------


def test_mode_defaults_to_copy_and_round_trips(tmp_path: Path) -> None:
    cfg = AppConfig()
    assert cfg.mode is AppMode.COPY
    cfg.mode = AppMode.TRADING
    cfg.feed_terminal_id = "F" * 32
    path = tmp_path / "config.json"
    save(cfg, path)
    loaded = load(path)
    assert loaded.mode is AppMode.TRADING
    assert loaded.feed_terminal_id == "F" * 32


def test_trading_mode_answers_with_the_virtual_master() -> None:
    slave = TerminalConfig(terminal_id="S" * 32, role=Role.SLAVE)
    cfg = AppConfig(mode=AppMode.TRADING, terminals={slave.terminal_id: slave})
    master = cfg.master
    assert master is not None and master.terminal_id == VIRTUAL_MASTER_ID
    assert master.role is Role.MASTER
    # The one-master rule and the orphaned-slave repair both key off this answer.
    assert cfg.manages(slave) is True


def test_a_trading_profile_keeps_its_slaves_on_load(tmp_path: Path) -> None:
    """The orphaned-slave repair must read the virtual master as a master, or loading a
    trading profile would silently unassign every slave."""
    slave = TerminalConfig(terminal_id="S" * 32, role=Role.SLAVE)
    cfg = AppConfig(mode=AppMode.TRADING, terminals={slave.terminal_id: slave})
    path = tmp_path / "config.json"
    save(cfg, path)
    assert load(path).terminals["S" * 32].role is Role.SLAVE


def test_a_master_role_in_a_trading_profile_is_repaired_on_load(tmp_path: Path) -> None:
    cfg = AppConfig(terminals={"M" * 32: TerminalConfig(terminal_id="M" * 32, role=Role.MASTER)})
    path = tmp_path / "config.json"
    save(cfg, path)
    # Hand-edit the file into the contradictory shape: trading mode plus a master role.
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["mode"] = "trading"
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load(path)
    assert loaded.terminals["M" * 32].role is Role.UNASSIGNED
    assert loaded.master is not None and loaded.master.terminal_id == VIRTUAL_MASTER_ID


def test_set_role_refuses_a_master_while_trading() -> None:
    cfg = AppConfig(
        mode=AppMode.TRADING,
        terminals={"T" * 32: TerminalConfig(terminal_id="T" * 32)},
    )
    with pytest.raises(ValueError, match="trades directly"):
        cfg.set_role("T" * 32, Role.MASTER)


def test_display_name_knows_the_virtual_master() -> None:
    assert AppConfig().display_name(VIRTUAL_MASTER_ID) == "Direct trading"


# --- the policy remembered per account ---------------------------------------------------------


def _on_a(**kw: object) -> AppConfig:
    tcfg = TerminalConfig(
        terminal_id=TID_A,
        last_login=111,
        last_server="A-Demo",
        **kw,  # type: ignore[arg-type]
    )
    return AppConfig(terminals={TID_A: tcfg})


def test_every_policy_field_is_a_real_setting() -> None:
    for field in POLICY_FIELDS:
        assert field in TerminalConfig.model_fields, field


def test_what_is_not_policy_stays_off_the_list() -> None:
    # Broker facts survive a switch, plumbing is the install's, and the typed live consent
    # must never ride back on the memory.
    for field in (
        "symbol_prefix",
        "symbol_suffix",
        "symbol_map",
        "max_deviation_points",
        "allow_live_trading",
        "role",
        "paused",
        "label",
        "exe_path",
        "last_login",
        "last_server",
    ):
        assert field not in POLICY_FIELDS, field


def test_a_switch_back_restores_the_settings_tuned_on_the_account() -> None:
    cfg = _on_a(risk_percent=2.5, invert=True)
    tcfg = cfg.terminals[TID_A]

    remember_policy(cfg, tcfg)  # leaving A
    tcfg = record_login(tcfg, 222, "B-Demo")
    tcfg = tcfg.model_copy(update={"risk_percent": 0.5, "invert": False})

    remember_policy(cfg, tcfg)  # leaving B
    tcfg = record_login(tcfg, 111, "A-Demo")
    tcfg, restored = restore_policy(cfg, tcfg)
    assert restored
    assert (tcfg.risk_percent, tcfg.invert) == (2.5, True)

    tcfg, restored = restore_policy(cfg, record_login(tcfg, 222, "B-Demo"))
    assert restored
    assert (tcfg.risk_percent, tcfg.invert) == (0.5, False)


def test_with_nothing_remembered_a_switch_keeps_the_current_settings() -> None:
    cfg = _on_a(risk_percent=2.5)
    tcfg, restored = restore_policy(cfg, cfg.terminals[TID_A])
    assert not restored
    assert tcfg is cfg.terminals[TID_A]


def test_remembering_with_no_known_account_is_a_no_op() -> None:
    cfg = AppConfig(terminals={TID_A: TerminalConfig(terminal_id=TID_A)})
    remember_policy(cfg, cfg.terminals[TID_A])
    assert cfg.account_policies == {}


def test_restore_takes_only_policy_keys_from_a_snapshot() -> None:
    cfg = _on_a(symbol_suffix=".x")
    cfg.account_policies["111@A-Demo"] = {
        "risk_percent": 3.0,
        "symbol_suffix": ".y",  # hand-edited in: not policy, must not apply
        "from_the_future": 7,  # a later version's field: ignored, not fatal
    }
    tcfg, restored = restore_policy(cfg, cfg.terminals[TID_A])
    assert restored
    assert tcfg.risk_percent == 3.0
    assert tcfg.symbol_suffix == ".x", "broker facts are not policy and stay put"


def test_a_snapshot_the_schema_rejects_restores_nothing() -> None:
    cfg = _on_a()
    cfg.account_policies["111@A-Demo"] = {"risk_percent": -3}
    tcfg, restored = restore_policy(cfg, cfg.terminals[TID_A])
    assert not restored
    assert tcfg.risk_percent == 1.0, "nothing half-applied"


def test_live_consent_is_never_restored_by_the_memory() -> None:
    cfg = _on_a(allow_live_trading=True, risk_percent=2.5)
    tcfg = cfg.terminals[TID_A]
    remember_policy(cfg, tcfg)
    tcfg = record_login(tcfg, 222, "B-Demo")
    assert not tcfg.allow_live_trading, "withdrawn by the switch, as before"
    tcfg, restored = restore_policy(cfg, record_login(tcfg, 111, "A-Demo"))
    assert restored
    assert tcfg.risk_percent == 2.5
    assert not tcfg.allow_live_trading, "the typed consent does not ride back"


def test_an_external_switch_seen_by_discovery_banks_the_settings(tmp_path: Path) -> None:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    cfg = _on_a(risk_percent=2.5)
    cfg.terminals[TID_A].exe_path = exe
    merge_discovery(cfg, [disc(TID_A, "Spare", exe, login=222, server="B-Demo")])
    assert cfg.account_policies["111@A-Demo"]["risk_percent"] == 2.5
    assert cfg.terminals[TID_A].last_login == 222


def test_a_sweep_reporting_the_same_account_banks_nothing(tmp_path: Path) -> None:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    cfg = _on_a(risk_percent=2.5)
    cfg.terminals[TID_A].exe_path = exe
    merge_discovery(cfg, [disc(TID_A, "Spare", exe, login=111, server="A-Demo")])
    assert cfg.account_policies == {}


def test_the_remembered_policies_survive_save_and_load(tmp_path: Path) -> None:
    cfg = _on_a(risk_percent=2.5)
    remember_policy(cfg, cfg.terminals[TID_A])
    path = tmp_path / "config.json"
    save(cfg, path)
    back = load(path)
    assert back.account_policies["111@A-Demo"]["risk_percent"] == 2.5
    tcfg, restored = restore_policy(back, back.terminals[TID_A])
    assert restored and tcfg.risk_percent == 2.5
