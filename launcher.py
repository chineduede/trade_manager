"""Entry script for the frozen build.

PyInstaller executes its entry script as top-level ``__main__``, which discards package
context -- so any relative import inside that script fails with "attempted relative import
with no known parent package". The real entry point therefore stays in
``mt5copier.__main__``, and this file only reaches it through an absolute import.

``freeze_support()`` is called before anything else is imported. Under the spawn start method
each worker re-launches this exe, and freeze_support is what makes the child run the worker
function instead of re-entering the UI and spawning workers of its own.
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from mt5copier.__main__ import main

    sys.exit(main())
