"""The role-change rules behind the UI's role buttons -- see ``role_actions.py``'s own
docstring for why this is one module instead of a copy per view."""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier.config import AppConfig, Role, TerminalConfig, account_key
from mt5copier.role_actions import set_master, set_slave, unassign

TID_A = "A" * 32
TID_B = "B" * 32


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig(
        terminals={
            TID_A: TerminalConfig(terminal_id=TID_A, role=Role.MASTER, exe_path=Path("a.exe")),
            TID_B: TerminalConfig(terminal_id=TID_B, exe_path=Path("b.exe")),
        }
    )


def test_set_master_refuses_while_another_terminal_is_the_master(cfg: AppConfig) -> None:
    """Replacing the master takes two deliberate steps: unassign it, then assign the new one."""
    refusal = set_master(cfg, TID_B)
    assert refusal is not None
    assert "already the master" in refusal and "unassign it" in refusal
    assert cfg.terminals[TID_A].role is Role.MASTER
    assert cfg.terminals[TID_B].role is Role.UNASSIGNED


def test_set_master_goes_in_once_the_old_master_is_unassigned(cfg: AppConfig) -> None:
    unassign(cfg, TID_A)
    assert set_master(cfg, TID_B) is None
    assert cfg.terminals[TID_B].role is Role.MASTER


def test_set_master_on_the_master_is_a_no_op(cfg: AppConfig) -> None:
    assert set_master(cfg, TID_A) is None
    assert cfg.terminals[TID_A].role is Role.MASTER


def test_set_master_refuses_with_no_executable(cfg: AppConfig) -> None:
    unassign(cfg, TID_A)
    cfg.terminals[TID_B].exe_path = None
    refusal = set_master(cfg, TID_B)
    assert refusal is not None
    assert "no terminal executable" in refusal
    assert cfg.terminals[TID_B].role is Role.UNASSIGNED


def test_set_slave_refuses_on_the_master(cfg: AppConfig) -> None:
    refusal = set_slave(cfg, TID_A)
    assert refusal is not None
    assert "unassign it first" in refusal
    assert TID_A[:8] in refusal, "the refusal must name which terminal, not just say what's wrong"
    assert cfg.terminals[TID_A].role is Role.MASTER


def test_set_slave_is_explicit_not_a_toggle(cfg: AppConfig) -> None:
    """Pressing it twice keeps a slave a slave; taking the role away is unassign's job."""
    assert set_slave(cfg, TID_B) is None
    assert cfg.terminals[TID_B].role is Role.SLAVE
    assert set_slave(cfg, TID_B) is None
    assert cfg.terminals[TID_B].role is Role.SLAVE


def test_set_slave_refuses_with_no_executable(cfg: AppConfig) -> None:
    cfg.terminals[TID_B].exe_path = None
    refusal = set_slave(cfg, TID_B)
    assert refusal is not None
    assert "no terminal executable" in refusal


def test_unassign_never_refuses_even_on_the_master(cfg: AppConfig) -> None:
    unassign(cfg, TID_A)
    assert cfg.terminals[TID_A].role is Role.UNASSIGNED


def test_set_slave_refuses_with_no_master() -> None:
    cfg = AppConfig(terminals={TID_B: TerminalConfig(terminal_id=TID_B, exe_path=Path("b.exe"))})
    refusal = set_slave(cfg, TID_B)
    assert refusal is not None
    assert "no master is assigned yet" in refusal
    assert TID_B[:8] in refusal, "the refusal must name which terminal, not just say what's wrong"
    assert cfg.terminals[TID_B].role is Role.UNASSIGNED


def test_unassign_sweeps_slaves_when_the_master_goes(cfg: AppConfig) -> None:
    assert set_slave(cfg, TID_B) is None
    assert cfg.terminals[TID_B].role is Role.SLAVE

    swept = unassign(cfg, TID_A)
    assert swept == (TID_B,)
    assert cfg.terminals[TID_A].role is Role.UNASSIGNED
    assert cfg.terminals[TID_B].role is Role.UNASSIGNED


def test_unassign_does_not_sweep_anything_when_unassigning_a_slave(cfg: AppConfig) -> None:
    assert set_slave(cfg, TID_B) is None

    swept = unassign(cfg, TID_B)
    assert swept == ()
    assert cfg.terminals[TID_A].role is Role.MASTER  # untouched


def test_refusals_name_the_logged_in_accounts_custom_name_when_one_is_set(cfg: AppConfig) -> None:
    """A persisted rename should show up in a refusal exactly like it shows up everywhere else
    a terminal's name is displayed -- not just the discovery label it overrides."""
    cfg.terminals[TID_A].last_login = 123
    cfg.terminals[TID_A].last_server = "A-Live"
    cfg.account_names[account_key(123, "A-Live")] = "My Funded Account"
    refusal = set_slave(cfg, TID_A)
    assert refusal is not None
    assert "My Funded Account" in refusal
