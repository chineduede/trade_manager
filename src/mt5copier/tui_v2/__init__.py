"""The redesigned UI, launched with ``mt5copier --new-ui``.

It is the same app as :mod:`mt5copier.tui` with a different face: :class:`NewCopierApp`
subclasses :class:`~mt5copier.tui.app.CopierApp` and replaces only how things are laid out and
drawn. Every action -- assigning roles, unassigning, settings, pausing, panic -- is the classic
app's own method, so the two cannot drift apart on anything that touches a trade.
"""

from .app import NewCopierApp, run

__all__ = ["NewCopierApp", "run"]
