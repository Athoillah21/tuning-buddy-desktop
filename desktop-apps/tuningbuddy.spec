# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Tuning Buddy (onedir). Run through build.ps1, which stages the
# services into build/stage/ and runs collectstatic first.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

HERE = Path(SPECPATH)
STAGE = HERE / "build" / "stage"
sys.path.insert(0, str(STAGE))  # so collect_submodules can find the staged packages

datas = [
    (str(STAGE / "advisor" / "templates"), "advisor/templates"),
    (str(STAGE / "staticfiles"), "staticfiles"),  # served by WhiteNoise
    (str(STAGE / "static"), "static"),            # STATICFILES_DIRS; avoids a startup warning
    # Seeds for the optional demo database (launcher/demo_db.py), the same ones Docker's sampledb runs
    (str(HERE.parent / "dockerize-microservices" / "infra" / "sampledb" / "init"), "demo_seeds"),
]
binaries = []
hiddenimports = ["sqlalchemy.dialects.sqlite"]

# Admin and form templates, and the default-language catalog Django insists on
datas += collect_data_files("django")
# Django loads apps, middleware, backends and migrations by dotted name
hiddenimports += collect_submodules(
    "django", filter=lambda name: not name.startswith(("django.contrib.gis", "django.test")) and ".tests" not in name
)
for package in ("advisor", "tuning_buddy", "ai_service", "analyzer_service", "report_service", "whitenoise"):
    hiddenimports += collect_submodules(package)

# AI SDKs import much of themselves lazily; reportlab needs its fonts; certifi its CA bundle
for package in ("anthropic", "openai", "google.genai", "reportlab", "certifi"):
    # include_py_files=False: the modules are compiled into the archive already, and the raw
    # sources would add ~3,000 files with paths long enough to break installs in deep folders
    package_datas, package_binaries, package_hiddenimports = collect_all(package, include_py_files=False)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    [str(HERE / "tuningbuddy.py")],
    pathex=[str(HERE), str(STAGE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "numpy", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TuningBuddy",
    icon=str(HERE / "assets" / "icon.ico"),
    console=False,
    upx=False,  # UPX-packed binaries trip antivirus heuristics
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="TuningBuddy",
    upx=False,
)
