# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the custom setup window (setup_ui/), onefile. build.ps1 runs it twice:
#   TB_SETUP_TARGET=uninstall  ->  TuningBuddyUninstall.exe (no payload; shipped inside the app)
#   TB_SETUP_TARGET=setup      ->  TuningBuddySetup.exe (with the Inno engine from build/engine/)
# TB_SETUP_VERSION sets the version shown in the exe's properties.
import os
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

HERE = Path(SPECPATH)
TARGET = os.environ.get("TB_SETUP_TARGET", "setup")
VERSION = os.environ.get("TB_SETUP_VERSION", "0.0.0")
NAME = "TuningBuddyUninstall" if TARGET == "uninstall" else "TuningBuddySetup"
DESCRIPTION = "Uninstall Tuning Buddy" if TARGET == "uninstall" else "Tuning Buddy Setup"

datas = [(str(HERE / "setup_ui" / "web"), "setup_ui/web")]
if TARGET == "setup":
    engine = HERE / "build" / "engine"
    datas += [(str(engine / "TuningBuddyEngine.exe"), "engine"), (str(engine / "engine.json"), "engine")]

numbers = tuple(int(p) for p in (VERSION.split(".") + ["0", "0", "0", "0"])[:4])
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Tuning Buddy"),
            StringStruct("FileDescription", DESCRIPTION),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", NAME),
            StringStruct("OriginalFilename", f"{NAME}.exe"),
            StringStruct("ProductName", "Tuning Buddy"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

a = Analysis(
    [str(HERE / "setup_ui" / "__main__.py")],
    pathex=[str(HERE)],
    datas=datas,
    hiddenimports=["setup_ui.api", "setup_ui.engine", "setup_ui.installs", "setup_ui.main"],
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "numpy", "PIL", "django", "fastapi", "sqlalchemy",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3", "reportlab", "anthropic", "openai",
              "google", "cryptography", "psycopg2"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    icon=str(HERE / "assets" / "icon.ico"),
    version=version_info,
    console=False,
    upx=False,  # UPX-packed binaries trip antivirus heuristics
    runtime_tmpdir=None,
)
