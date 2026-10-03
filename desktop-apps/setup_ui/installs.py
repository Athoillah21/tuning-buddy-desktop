"""
What the installer needs to know about this PC: an existing Tuning Buddy install (from the
registry entry the engine writes), whether the app is running, free disk space, the size of the
user's data, and whether WebView2 is there to draw the custom window.
"""
import ctypes
import os
import shlex
import shutil
from pathlib import Path
from typing import Dict, Optional

try:
    import winreg
except ImportError:  # only for running the unit tests elsewhere
    winreg = None

APP_NAME = "Tuning Buddy"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{6B0E4C2A-9E57-4F1B-A7D3-5C2E8B1F0A94}_is1"
APP_MUTEX = "Local\\TuningBuddy.SingleInstance"
WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

# Sizes for the disk check: the installed app, and the demo database with headroom for its WAL
APP_BYTES = 450 * 1024 ** 2
DEMO_BYTES = 650 * 1024 ** 2


def _read_values(root, path: str, view: int) -> Optional[Dict[str, str]]:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | view) as key:
            values, index = {}, 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    return values
                values[name] = value
                index += 1
    except OSError:
        return None


def find_install() -> Optional[Dict[str, str]]:
    """
    The installed copy, if any: scope ('user' or 'all'), version, folder, and the stock
    uninstaller (unins000.exe) the premium uninstaller runs. A just-for-me install wins when
    (unusually) there are both, like the old uninstaller stub.
    """
    if winreg is None:
        return None
    for scope, root, view in (("user", winreg.HKEY_CURRENT_USER, 0),
                              ("all", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                              ("all", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        values = _read_values(root, UNINSTALL_KEY, view)
        if not values:
            continue
        location = (values.get("InstallLocation") or values.get("Inno Setup: App Path") or "").rstrip("\\/")
        return {
            "scope": scope,
            "version": values.get("DisplayVersion", ""),
            "location": location,
            "uninstaller": stock_uninstaller(values, location),
        }
    return None


def stock_uninstaller(values: Dict[str, str], location: str) -> str:
    """unins000.exe: from QuietUninstallString (the premium uninstaller replaced UninstallString)."""
    for name in ("QuietUninstallString", "UninstallString"):
        command = values.get(name) or ""
        if command:
            try:
                program = shlex.split(command, posix=False)[0].strip('"')
            except ValueError:
                continue
            if Path(program).name.lower().startswith("unins"):
                return program
    return str(Path(location) / "unins000.exe") if location else ""


def default_folder(scope: str) -> str:
    """Where Inno's {autopf} puts the app for each scope."""
    if scope == "user":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return str(Path(base) / "Programs" / APP_NAME)
    return str(Path(os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files") / APP_NAME)


def app_running() -> bool:
    """The app holds this mutex for as long as it runs."""
    if os.name != "nt":
        return False
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, APP_MUTEX)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def free_bytes(folder: str) -> int:
    """Free space on the drive of `folder` (which may not exist yet)."""
    path = Path(folder)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def needed_bytes(demo: bool) -> int:
    return APP_BYTES + (DEMO_BYTES if demo else 0)


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "TuningBuddy"


def folder_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def webview2_available() -> bool:
    """The Evergreen WebView2 runtime registers itself here (per machine or per user)."""
    if winreg is None:
        return False
    if os.environ.get("TB_SETUP_NO_WEBVIEW"):  # for testing the fallback wizard
        return False
    for root, path, view in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT, 0),
                             (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WEBVIEW2_CLIENT, 0),
                             (winreg.HKEY_CURRENT_USER, "Software\\" + WEBVIEW2_CLIENT, 0)):
        values = _read_values(root, path, view)
        if values and values.get("pv") and values["pv"] != "0.0.0.0":
            return True
    return False


def windows_uses_dark_theme() -> bool:
    """Used only to paint the window's first frame in the right colour (no white flash)."""
    if winreg is None:
        return False
    values = _read_values(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", 0) or {}
    return values.get("AppsUseLightTheme", 1) == 0
