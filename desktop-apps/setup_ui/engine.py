"""
Run the Inno Setup engine (or the stock uninstaller) silently and turn what it reports into a
status the window can show.

The engine rewrites a small JSON file (/PROGRESSFILE) as it goes: its phase and the file-copy
percentage. The demo database setup appends one line per step to <progressfile>.demo. When the
process exits, its exit code says how it ended.
"""
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

CREATE_NO_WINDOW = 0x08000000

# Inno Setup's documented exit codes, as the window explains them
EXIT_MESSAGES = {
    1: "Setup could not start.",
    2: "Setup was cancelled before anything was installed.",
    3: "Setup stopped while preparing to install.",
    4: "Something went wrong while copying files.",
    # Nothing can cancel a silent run, so 5 means an error box that /SUPPRESSMSGBOXES answered with Abort
    5: "Setup couldn't write one of its files and stopped, so nothing was changed. Show details says which one.",
    7: "Setup could not continue: files are in use. Close Tuning Buddy and try again.",
    8: "Windows needs to restart before Setup can continue.",
}

# The phases in order, with the overall share of the progress bar each one covers
PHASES = ("preparing", "files", "demo", "finishing", "done")
# After the window's Cancel: "cancelling" (rolling back), "removing" (a new install removing itself
# again after its files were in place) and "cancelled" (gone again)


def read_progress(path: Path) -> Dict[str, Any]:
    """The engine's last snapshot. It rewrites the whole file, so a read can catch it half-written."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def read_lines(path: Path) -> List[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def overall_percent(phase: str, percent: int, demo: bool) -> int:
    """
    One number for the progress ring. File copying is most of the work; with the demo database,
    loading it takes about as long again, so it gets its own share.
    """
    if phase == "done":
        return 100
    files_share = 55 if demo else 95
    if phase == "preparing":
        return 2
    if phase == "files":
        return 3 + int(files_share * max(0, min(percent, 100)) / 100)
    if phase == "demo":
        return 3 + files_share + (20 if demo else 0)  # nudged forward by demo lines, see below
    if phase == "finishing":
        return 99
    if phase in ("cancelling", "removing", "cancelled"):
        return percent  # the ring stays where it was; the page shows it winding down
    return 0


class Run:
    """One silent engine run. `status()` is safe to call from the UI thread at any time."""

    def __init__(self, program: str, args: List[str], demo: bool = False, kind: str = "install"):
        self.kind = kind
        self.demo = demo
        self.workdir = Path(tempfile.mkdtemp(prefix="tb-setup-"))
        self.progress_path = self.workdir / "progress.json"
        self.demo_path = self.workdir / "progress.json.demo"
        self.log_path = self.workdir / "engine.log"
        self.command = [program, *args]
        if kind == "install":
            self.command += [f"/PROGRESSFILE={self.progress_path}", f"/LOG={self.log_path}"]
        self.exit_code: Optional[int] = None
        self.started_at = time.time()
        self.error: Optional[str] = None
        self._process: Optional[subprocess.Popen] = None

    def start(self) -> None:
        try:
            self._process = subprocess.Popen(self.command, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            self.exit_code, self.error = -1, f"Could not start {Path(self.command[0]).name}: {e}"
            return
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self) -> None:
        self.exit_code = self._process.wait()

    def status(self) -> Dict[str, Any]:
        snapshot = read_progress(self.progress_path)
        phase = snapshot.get("phase", "starting")
        demo_lines = read_lines(self.demo_path)
        finished = self.exit_code is not None
        percent = overall_percent(phase, int(snapshot.get("percent", 0) or 0), self.demo)
        if phase == "demo" and self.demo:
            # Six seed files: move the ring forward as each one is loaded
            percent = min(97, percent + 3 * len(demo_lines))
        result = {
            "kind": self.kind,
            "phase": phase,
            "percent": percent,
            "file_percent": int(snapshot.get("percent", 0) or 0),
            "demo_lines": demo_lines,
            "demo_result": snapshot.get("demo", ""),
            "finished": finished,
            "exit_code": self.exit_code,
            "elapsed": round(time.time() - self.started_at, 1),
        }
        if finished:
            result["ok"] = self.exit_code == 0
            if self.exit_code != 0:
                result["error"] = self.error or self._explain(phase)
        return result

    def _explain(self, phase: str) -> str:
        if self.kind == "uninstall":
            return ("Tuning Buddy could not be removed. If Windows asked for administrator permission, "
                    "it may have been declined.")
        if phase == "starting":
            # The engine never got as far as preparing: the admin prompt was declined, or it failed early
            return ("Setup didn't start. If Windows asked for administrator permission, it may have been "
                    "declined. To install without it, choose Just me.")
        return EXIT_MESSAGES.get(self.exit_code, f"Setup stopped unexpectedly (code {self.exit_code}).")

    def log_tail(self, lines: int = 40) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "No log was written."
        return "\n".join(text.splitlines()[-lines:])

    def cleanup(self) -> None:
        for path in (self.progress_path, self.demo_path):
            try:
                os.remove(path)
            except OSError:
                pass
