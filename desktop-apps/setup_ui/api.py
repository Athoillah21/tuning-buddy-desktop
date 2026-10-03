"""
The window's JavaScript calls these (window.pywebview.api.*). Everything returns plain dicts.

Install: info() → check(options) → start(options) → status() until finished → launch() / close().
         cancel() at any point before "finishing".
Uninstall: info() → start({delete_data}) → status() until finished → close().
"""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from setup_ui import glass, installs
from setup_ui.engine import CREATE_NO_WINDOW, Run

DETACHED_PROCESS = 0x00000008
SILENT = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
# /SILENT, not /VERYSILENT: only then can Setup be cancelled (the engine makes its window invisible)
INSTALL_SILENT = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]


def _gb(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{max(1, round(n / 1024 ** 2))} MB"


class _Window:
    """Window controls shared by both modes."""

    _window = None
    _hwnd = 0
    _glass = False  # the window is clear glass (glass.py); set by main.py
    _glass_refreshed = 0.0

    def set_dark(self, dark: bool) -> None:
        """The page follows Windows light/dark; the window's border and shadow have to be told."""
        if self._glass and self._hwnd:
            glass.set_dark(self._hwnd, bool(dark))

    def _keep_glass(self) -> None:
        """While work runs, put the glass back about once a second, in case Windows dropped it."""
        if self._glass and self._hwnd and time.time() - self._glass_refreshed > 1:
            self._glass_refreshed = time.time()
            glass.refresh(self._hwnd)

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def close(self) -> None:
        if self._window:
            self._window.destroy()


class InstallApi(_Window):
    def __init__(self, engine_dir: Path):
        self._engine = engine_dir / "TuningBuddyEngine.exe"
        try:
            self._manifest = json.loads((engine_dir / "engine.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._manifest = {}
        self._run: Optional[Run] = None
        self._folder: str = ""
        self._cancelled = False
        self._updating = False

    def info(self) -> Dict[str, Any]:
        existing = installs.find_install()
        version = self._manifest.get("version", "")
        result = {
            "mode": "install",
            "glass": self._glass,
            "version": version,
            "demo_available": bool(self._manifest.get("demo")),
            "defaults": {"all": installs.default_folder("all"), "user": installs.default_folder("user")},
            "existing": existing,
            "action": "install",
        }
        if existing:
            result["action"] = "reinstall" if existing.get("version") == version else "update"
        return result

    def check(self, options: Dict[str, Any]) -> Dict[str, Any]:
        """Pre-flight: is the app running, and is there room for what was chosen?"""
        folder = options.get("folder") or installs.default_folder(options.get("scope", "all"))
        demo = bool(options.get("demo"))
        free, needed = installs.free_bytes(folder), installs.needed_bytes(demo)
        return {
            "app_running": installs.app_running(),
            "free": free,
            "needed": needed,
            "disk_ok": free >= needed,
            "free_text": _gb(free),
            "needed_text": _gb(needed),
        }

    def choose_folder(self, current: str) -> Optional[str]:
        import webview
        start = current
        while start and not os.path.isdir(start):
            start = os.path.dirname(start) if os.path.dirname(start) != start else ""
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=start or "")
        if not picked:
            return None
        folder = picked[0] if isinstance(picked, (list, tuple)) else picked
        # Like Inno's folder page: picking "D:\Apps" means "D:\Apps\Tuning Buddy"
        if Path(folder).name.lower() != installs.APP_NAME.lower():
            folder = str(Path(folder) / installs.APP_NAME)
        return folder

    def start(self, options: Dict[str, Any]) -> Dict[str, Any]:
        if self._run and self._run.exit_code is None:
            return {"started": False, "error": "Setup is already running."}
        scope = "user" if options.get("scope") == "user" else "all"
        self._folder = options.get("folder") or installs.default_folder(scope)
        tasks = []
        demo = bool(options.get("demo")) and bool(self._manifest.get("demo"))
        if demo:
            tasks.append("demodb")
        if options.get("shortcut"):
            tasks.append("desktopicon")
        self._cancelled = False
        self._updating = installs.find_install() is not None
        args = INSTALL_SILENT + ["/ALLUSERS" if scope == "all" else "/CURRENTUSER",
                         f"/DIR={self._folder}", f"/TASKS={','.join(tasks)}"]
        self._run = Run(str(self._engine), args, demo=demo, kind="install")
        self._run.start()
        return {"started": True}

    def cancel(self) -> Dict[str, Any]:
        """
        Ask the engine (and the demo database setup) to stop: they watch for <progress file>.cancel.
        While files are copied, Setup rolls back. Later, a new install removes itself again and an
        update keeps the new version without the demo database.
        """
        run = self._run
        if not run or run.exit_code is not None:
            return {"cancelling": False}
        self._cancelled = True
        for path in (run.progress_path, run.demo_path):
            try:
                Path(f"{path}.cancel").write_text("", encoding="utf-8")
            except OSError:
                pass
        return {"cancelling": True}

    def status(self) -> Dict[str, Any]:
        self._keep_glass()
        if not self._run:
            return {"phase": "idle"}
        status = self._run.status()
        status["updating"] = self._updating
        if not self._cancelled:
            return status
        status["cancelling"] = True
        if status["finished"]:
            # Rolled back (exit 5), stopped before starting (1/7), or removed again ("cancelled"):
            # nothing is left. An update that was already in place when Cancel came stays.
            kept = bool(status.get("ok")) and status.get("phase") != "cancelled"
            status.update({"cancelled": True, "kept": kept, "ok": kept, "error": None})
        return status

    def details(self) -> str:
        return self._run.log_tail() if self._run else ""

    def launch(self) -> bool:
        exe = Path(self._folder) / "TuningBuddy.exe"
        if not exe.is_file():
            return False
        subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=DETACHED_PROCESS, close_fds=True)
        self.close()
        return True


class UninstallApi(_Window):
    """
    Runs the stock uninstaller (unins000.exe) silently, then deletes the user's data if asked.
    The phases are this class's own: the stock uninstaller reports nothing while it runs.
    """

    def __init__(self):
        self._install = installs.find_install()
        self._phase = "idle"
        self._error: Optional[str] = None
        self._delete_data = False
        self._data_deleted = False
        self._started_at = 0.0
        self._thread: Optional[threading.Thread] = None

    def info(self) -> Dict[str, Any]:
        data = installs.data_dir()
        size = installs.folder_size(data) if data.is_dir() else 0
        return {
            "mode": "uninstall",
            "glass": self._glass,
            "existing": self._install,
            "version": (self._install or {}).get("version", ""),
            "data_dir": str(data),
            "data_exists": data.is_dir(),
            "data_size": size,
            "data_size_text": _gb(size) if size else "",
        }

    def check(self, options: Dict[str, Any] = None) -> Dict[str, Any]:
        return {"app_running": installs.app_running()}

    def start(self, options: Dict[str, Any]) -> Dict[str, Any]:
        if self._thread and self._thread.is_alive():
            return {"started": False}
        self._delete_data = bool(options.get("delete_data"))
        self._error, self._data_deleted = None, False
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()
        return {"started": True}

    def _work(self) -> None:
        location = Path(self._install["location"]) if self._install else None
        uninstaller = (self._install or {}).get("uninstaller", "")

        # 1. The demo server, stopped as this user (it runs from their profile)
        self._phase = "stopping"
        app_exe = location / "TuningBuddy.exe" if location else None
        if app_exe and (location / "pgsql" / "bin" / "pg_ctl.exe").is_file() and app_exe.is_file():
            try:
                subprocess.run([str(app_exe), "--stop-demo"], creationflags=CREATE_NO_WINDOW, timeout=90)
            except (OSError, subprocess.SubprocessError):
                pass  # the stock uninstaller tries again
        time.sleep(0.6)  # long enough to read the line; stopping is usually instant

        # 2. The app itself
        if self._install:
            self._phase = "removing"
            if not uninstaller or not os.path.isfile(uninstaller):
                self._phase, self._error = "error", f"The uninstaller is missing ({uninstaller or 'unknown'})."
                return
            run = Run(uninstaller, SILENT, kind="uninstall")
            run.start()
            while run.exit_code is None:
                time.sleep(0.2)
            # The stock uninstaller hands over to a copy of itself; give it a moment to finish
            deadline = time.time() + 60
            while installs.find_install() and time.time() < deadline:
                time.sleep(0.3)
            if installs.find_install():
                self._phase, self._error = "error", run.status().get("error") or (
                    "Tuning Buddy could not be removed. If Windows asked for administrator permission, "
                    "it may have been declined.")
                return

        # 3. The user's data, only if asked; only this user's folder
        if self._delete_data:
            self._phase = "data"
            data = installs.data_dir()
            if data.is_dir():
                shutil.rmtree(data, ignore_errors=True)
            self._data_deleted = not data.exists()
            time.sleep(0.4)
        self._phase = "done"

    def status(self) -> Dict[str, Any]:
        self._keep_glass()
        finished = self._phase in ("done", "error")
        result = {
            "kind": "uninstall",
            "phase": self._phase,
            "finished": finished,
            "delete_data": self._delete_data,
            "data_deleted": self._data_deleted,
            "data_dir": str(installs.data_dir()),
            "elapsed": round(time.time() - self._started_at, 1) if self._started_at else 0,
        }
        if finished:
            result["ok"] = self._phase == "done"
            if self._error:
                result["error"] = self._error
        return result

    def details(self) -> str:
        return self._error or ""
