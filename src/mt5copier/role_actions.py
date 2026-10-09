"""Role changes, with no UI in them -- the same reason :mod:`mt5copier.settings_edit` exists.

Roles are set **explicitly**, never toggled: one call per role, each refusing anything that
would change a role the person did not point at.

* **A terminal with no executable cannot be connected to**, so giving it a role would show it as
  assigned while it can never actually run. Unassigning is exempt -- it never needs to connect.
* **The master is only ever replaced by unassigning it first.** :func:`set_master` refuses while
  another terminal holds the role, rather than quietly demoting it: swapping which account is
  the master is the single most consequential thing this app does, so it takes two deliberate
  steps, not one press on the wrong row. For the same reason the master is never made a slave
  directly -- "the funded account is now being sized as a slave" is one keypress away otherwise.
* **A slave with no master makes no sense** -- there is nothing for it to copy from -- so
  :func:`set_slave` refuses to create one, and :func:`unassign` sweeps every existing slave
  along with the master, in the same call, rather than ever leave that state on the table even
  for a moment.

Pulled out here, rather than left inline, so the keyboard shortcuts and the role buttons in the
table make exactly the same decision instead of two copies drifting apart -- nothing about
*what* a role change does to open copies changes: that is a different decision, in
``Engine.apply_settings``, and role changes do not go through it today (a known, pre-existing
gap, not something introduced here).

Every refusal names the terminal: it lands in a toast next to a table listing every terminal,
where a message with no name in it is useless -- so the label belongs here, not bolted on by
each caller.
"""

from __future__ import annotations

from .config import AppConfig, Role


def set_master(cfg: AppConfig, terminal_id: str) -> str | None:
    """Make this terminal the master. None means it went in (or it already was).

    Refuses while a different terminal is the master: unassign that one first.
    """
    tcfg = cfg.terminals[terminal_id]
    label = cfg.display_name(terminal_id)
    current = cfg.master
    if current is not None and current.terminal_id == terminal_id:
        return None
    if current is not None:
        held_by = cfg.display_name(current.terminal_id)
        return f"{held_by} is already the master; unassign it before making {label} the master"
    if tcfg.is_ctrader:
        return f"{label} is a cTrader account, which can only be a slave"
    if tcfg.exe_path is None:
        return f"{label} has no terminal executable"
    cfg.set_role(terminal_id, Role.MASTER)
    return None


def set_slave(cfg: AppConfig, terminal_id: str) -> str | None:
    """Make this terminal a slave. None means it went in (or it already was).

    Refuses on the master -- unassign it first -- and refuses while there is no master to copy.
    """
    tcfg = cfg.terminals[terminal_id]
    label = cfg.display_name(terminal_id)
    if tcfg.role is Role.SLAVE:
        return None
    if tcfg.role is Role.MASTER:
        return f"{label} is the master; unassign it first"
    if cfg.master is None:
        return f"no master is assigned yet; assign one before making {label} a slave"
    if not tcfg.usable:
        return f"{label} has no terminal executable"
    cfg.set_role(terminal_id, Role.SLAVE)
    return None


def unassign(cfg: AppConfig, terminal_id: str) -> tuple[str, ...]:
    """Unassign, from any role. Never refused -- it never needs an executable to take effect.

    If this terminal was the master, every current slave is swept to unassigned in the same
    call: a slave with no master is a state this app must never hold, even for a moment between
    this save and whatever role change happens next. Returns the terminal_ids of any slaves
    swept along, so the caller can tell the person what happened instead of slaves silently
    losing their role with no explanation.
    """
    was_master = cfg.terminals[terminal_id].role is Role.MASTER
    cfg.set_role(terminal_id, Role.UNASSIGNED)
    if not was_master:
        return ()
    swept = tuple(t.terminal_id for t in cfg.terminals.values() if t.role is Role.SLAVE)
    for tid in swept:
        cfg.set_role(tid, Role.UNASSIGNED)
    return swept
