"""The custom setup window's Python side: python -m pytest tests (from desktop-apps)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from setup_ui import api, engine, installs  # noqa: E402


# ---------------------------------------------------------------------- engine progress

def test_progress_file_half_written_reads_as_nothing(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text('{"phase": "files", "perc', encoding="utf-8")
    assert engine.read_progress(path) == {}
    assert engine.read_progress(tmp_path / "missing.json") == {}


def test_progress_file_snapshot(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text('{"phase": "demo", "percent": 100, "demo": "ok", "version": "1.3.4"}', encoding="utf-8")
    assert engine.read_progress(path)["phase"] == "demo"


def test_demo_lines_skip_blanks(tmp_path):
    path = tmp_path / "progress.json.demo"
    path.write_text("Creating the demo database server\n\n  Loading large_orders · 1M rows \n", encoding="utf-8")
    assert engine.read_lines(path) == ["Creating the demo database server", "Loading large_orders · 1M rows"]


@pytest.mark.parametrize("demo", [True, False])
def test_overall_percent_only_moves_forward_through_the_phases(demo):
    points = [engine.overall_percent("preparing", 0, demo)]
    points += [engine.overall_percent("files", p, demo) for p in range(0, 101, 10)]
    points += [engine.overall_percent("demo", 100, demo), engine.overall_percent("finishing", 100, demo),
               engine.overall_percent("done", 100, demo)]
    assert points == sorted(points)
    assert points[-1] == 100
    assert all(p < 100 for p in points[:-1])


def test_files_share_is_smaller_when_the_demo_follows():
    assert engine.overall_percent("files", 100, True) < engine.overall_percent("files", 100, False)


class FakeRun(engine.Run):
    def __init__(self, tmp_path, kind="install", demo=True):
        super().__init__("engine.exe", [], demo=demo, kind=kind)
        self.progress_path = tmp_path / "p.json"
        self.demo_path = tmp_path / "p.json.demo"
        self.log_path = tmp_path / "engine.log"


def test_status_while_waiting_for_windows(tmp_path):
    run = FakeRun(tmp_path)
    status = run.status()
    assert status["phase"] == "starting" and not status["finished"]


def test_status_demo_lines_push_the_ring_forward(tmp_path):
    run = FakeRun(tmp_path)
    run.progress_path.write_text(json.dumps({"phase": "demo", "percent": 100}), encoding="utf-8")
    before = run.status()["percent"]
    run.demo_path.write_text("a\nb\nc\n", encoding="utf-8")
    after = run.status()
    assert after["percent"] > before
    assert after["demo_lines"] == ["a", "b", "c"]


def test_declined_admin_prompt_is_explained(tmp_path):
    run = FakeRun(tmp_path)
    run.exit_code = 1  # the engine never reported a phase
    status = run.status()
    assert status["finished"] and not status["ok"]
    assert "Just me" in status["error"]


def test_known_exit_code_is_explained(tmp_path):
    run = FakeRun(tmp_path)
    run.progress_path.write_text(json.dumps({"phase": "files", "percent": 40}), encoding="utf-8")
    run.exit_code = 5
    assert run.status()["error"] == engine.EXIT_MESSAGES[5]


def test_success(tmp_path):
    run = FakeRun(tmp_path)
    run.progress_path.write_text(json.dumps({"phase": "done", "percent": 100, "demo": "ok"}), encoding="utf-8")
    run.exit_code = 0
    status = run.status()
    assert status["ok"] and status["percent"] == 100 and status["demo_result"] == "ok"


def test_log_tail(tmp_path):
    run = FakeRun(tmp_path)
    assert run.log_tail() == "No log was written."
    run.log_path.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
    assert run.log_tail(3) == "line 97\nline 98\nline 99"


def test_install_runs_add_the_progress_and_log_switches():
    run = engine.Run("engine.exe", ["/VERYSILENT"], kind="install")
    assert any(a.startswith("/PROGRESSFILE=") for a in run.command)
    assert any(a.startswith("/LOG=") for a in run.command)
    uninstall = engine.Run("unins000.exe", ["/VERYSILENT"], kind="uninstall")
    assert uninstall.command == ["unins000.exe", "/VERYSILENT"]


# ---------------------------------------------------------------------- installs

def test_stock_uninstaller_from_quiet_string_once_the_premium_one_took_over():
    values = {"UninstallString": '"C:\\Program Files\\Tuning Buddy\\TuningBuddyUninstall.exe"',
              "QuietUninstallString": '"C:\\Program Files\\Tuning Buddy\\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES'}
    assert installs.stock_uninstaller(values, "C:\\Program Files\\Tuning Buddy") == "C:\\Program Files\\Tuning Buddy\\unins000.exe"


def test_stock_uninstaller_from_an_older_install():
    values = {"UninstallString": '"C:\\Apps\\Tuning Buddy\\unins000.exe"', "QuietUninstallString": '"C:\\Apps\\Tuning Buddy\\unins000.exe" /SILENT'}
    assert installs.stock_uninstaller(values, "C:\\Apps\\Tuning Buddy") == "C:\\Apps\\Tuning Buddy\\unins000.exe"


def test_stock_uninstaller_falls_back_to_the_folder():
    assert installs.stock_uninstaller({}, "D:\\TB") == str(Path("D:\\TB") / "unins000.exe")
    assert installs.stock_uninstaller({}, "") == ""


def test_default_folders(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local")
    monkeypatch.setenv("ProgramW6432", "C:\\Program Files")
    assert installs.default_folder("user") == str(Path("C:\\Users\\x\\AppData\\Local\\Programs\\Tuning Buddy"))
    assert installs.default_folder("all") == str(Path("C:\\Program Files\\Tuning Buddy"))


def test_free_bytes_walks_up_to_an_existing_folder(tmp_path):
    assert installs.free_bytes(str(tmp_path / "not" / "yet" / "there")) > 0


def test_needed_bytes_counts_the_demo():
    assert installs.needed_bytes(True) > installs.needed_bytes(False) > 0


def test_folder_size(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x" * 1000)
    (tmp_path / "g").write_bytes(b"y" * 24)
    assert installs.folder_size(tmp_path) == 1024


def test_no_webview_override(monkeypatch):
    monkeypatch.setenv("TB_SETUP_NO_WEBVIEW", "1")
    assert installs.webview2_available() is False


# ---------------------------------------------------------------------- the install API

@pytest.fixture
def install_api(tmp_path, monkeypatch):
    (tmp_path / "engine.json").write_text('{"version": "1.3.4", "demo": true}', encoding="utf-8")
    monkeypatch.setattr(installs, "find_install", lambda: None)
    started = []
    monkeypatch.setattr(engine.Run, "start", lambda self: started.append(self.command))
    result = api.InstallApi(tmp_path)
    result.started = started
    return result


def test_install_command_line_for_everyone(install_api):
    install_api.start({"scope": "all", "folder": "C:\\Program Files\\Tuning Buddy", "demo": True, "shortcut": True})
    command = install_api.started[0]
    assert command[0].endswith("TuningBuddyEngine.exe")
    assert {"/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/ALLUSERS"} <= set(command)  # /SILENT can be cancelled
    assert "/DIR=C:\\Program Files\\Tuning Buddy" in command
    assert "/TASKS=demodb,desktopicon" in command


def test_install_command_line_just_me_without_tasks(install_api):
    install_api.start({"scope": "user", "folder": "", "demo": False, "shortcut": False})
    command = install_api.started[0]
    assert "/CURRENTUSER" in command and "/ALLUSERS" not in command
    assert "/TASKS=" in command
    assert any(a.startswith("/DIR=") and a.endswith("Tuning Buddy") for a in command)


def test_demo_is_not_asked_for_when_the_engine_has_none(install_api):
    install_api._manifest["demo"] = False
    install_api.start({"scope": "all", "demo": True})
    assert "/TASKS=" in install_api.started[0]


def test_info_actions(install_api, monkeypatch):
    assert install_api.info()["action"] == "install"
    monkeypatch.setattr(installs, "find_install", lambda: {"scope": "all", "version": "1.3.3", "location": "C:\\x", "uninstaller": ""})
    assert install_api.info()["action"] == "update"
    monkeypatch.setattr(installs, "find_install", lambda: {"scope": "all", "version": "1.3.4", "location": "C:\\x", "uninstaller": ""})
    assert install_api.info()["action"] == "reinstall"


def test_check_reports_low_disk(install_api, monkeypatch):
    monkeypatch.setattr(installs, "free_bytes", lambda folder: 100 * 1024 ** 2)
    monkeypatch.setattr(installs, "app_running", lambda: False)
    check = install_api.check({"scope": "all", "demo": True})
    assert check["disk_ok"] is False and check["free_text"] == "100 MB" and check["needed_text"] == "1.1 GB"


def test_only_methods_reach_javascript(tmp_path, monkeypatch):
    """pywebview walks every public attribute of js_api; state (the window, a run) must stay private."""
    monkeypatch.setattr(installs, "find_install", lambda: None)
    for obj in (api.InstallApi(tmp_path), api.UninstallApi()):
        obj._window = object()
        public = [name for name in dir(obj) if not name.startswith("_")]
        assert public and all(callable(getattr(obj, name)) for name in public), public
