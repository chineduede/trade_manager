# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for mt5copier.

Build with:  uv run pyinstaller copier.spec --noconfirm
Then ALWAYS read build/mt5copier/warn-mt5copier.txt before trusting the result.

Three things here are direct fixes for how the zele-mt5 exe came out broken:

1. ``MetaTrader5`` is spelled with its real capitalisation. PyInstaller's module graph is
   case-sensitive, so the lowercase ``"metatrader5"`` hidden import in that project pointed at
   a module that does not exist -- the real C extension was never collected, and the shipped
   17MB exe could not import it at runtime.
2. ``collect_dynamic_libs`` is called for it as well, because the package is a compiled
   extension rather than pure Python.
3. The build must run inside the project venv (``uv run``), not a global interpreter. That
   project's build script did a bare ``pip install pyinstaller`` and ended up analysing an
   environment that had no MetaTrader5, PyYAML or telethon in it at all.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# The MT5 package is a C extension; take its DLLs as well as the module itself.
mt5_binaries = collect_dynamic_libs("MetaTrader5")

# Textual ships its widget stylesheets as package data (.tcss); without these the app starts
# and then fails to render.
textual_datas = collect_data_files("textual")

datas = [
    *textual_datas,
    # The IANA time zones a guard's daily reset is set in. Windows has no system zone
    # database, so zoneinfo reads this package's files; without them every zone fails to load.
    *collect_data_files("tzdata"),
    # Our own stylesheet, at the path CSS_PATH resolves to inside the bundle.
    ("src/mt5copier/tui/app.tcss", "mt5copier/tui"),
]

hiddenimports = [
    "MetaTrader5",
    # MetaTrader5 pulls numpy in from inside its C extension, so the module graph does not see
    # the dependency. Without this the exe builds cleanly, reports nothing in warn-copier.txt,
    # and then fails at runtime with "numpy._core.multiarray failed to import". Caught by
    # `mt5copier.exe --check`.
    "numpy",
    # Spawned worker processes re-import these by name in the child interpreter.
    "mt5copier.worker",
    "mt5copier.protocol",
    "mt5copier.logging_setup",
]

a = Analysis(
    # launcher.py, NOT mt5copier/__main__.py: PyInstaller runs its entry script as top-level
    # __main__, which discards package context and makes every relative import in that module
    # fail at startup.
    ["launcher.py"],
    pathex=["src"],
    binaries=mt5_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Note: do not exclude numpy submodules here. Pruning them saves little and risks breaking
    # numpy's own import machinery, which MetaTrader5 depends on.
    excludes=["pytest", "pytest_cov", "pytest_asyncio", "ruff"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mt5copier",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed exes get flagged by AV more often; the size saving is not worth it
    runtime_tmpdir=None,
    # Console is required: this IS a terminal UI. A windowed build would have no tty and
    # Textual would fail to start.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
