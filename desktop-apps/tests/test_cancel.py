"""Cancel from the setup window: the API's side, and the demo database setup stopping between steps."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from setup_ui import api, engine, installs  # noqa: E402


@pytest.fixture
def started(tmp_path, monkeypatch):
    (tmp_path / "engine.json").write_text('{"version": "1.3.4", "demo": true}', encoding="utf-8")
    monkeypatch.setattr(installs, "find_install", lambda: None)
    monkeypatch.setattr(engine.Run, "start", lambda self: None)
    install = api.InstallApi(tmp_path)
    install.start({"scope": "user", "folder": str(tmp_path / "app"), "demo": True})
    run = install._run
    run.progress_path = tmp_path / "progress.json"
    run.demo_path = tmp_path / "progress.json.demo"
    return install, run


def finish(run, code, phase, **extra):
    run.progress_path.write_text(json.dumps({"phase": phase, "percent": 40, **extra}), encoding="utf-8")
    run.exit_code = code


def test_cancel_asks_the_engine_and_the_demo_setup(started):
    install, run = started
    assert install.cancel() == {"cancelling": True}
    assert Path(f"{run.progress_path}.cancel").exists()
    assert Path(f"{run.demo_path}.cancel").exists()
    status = install.status()
    assert status["cancelling"] and not status["finished"]


def test_cancel_after_the_engine_finished_does_nothing(started):
    install, run = started
    finish(run, 0, "done")
    assert install.cancel() == {"cancelling": False}
    assert not Path(f"{run.progress_path}.cancel").exists()


def test_rolled_back(started):
    install, run = started
    install.cancel()
    finish(run, 5, "cancelling")
    status = install.status()
    assert status["finished"] and status["cancelled"] and not status["kept"] and not status["ok"]
    assert status["error"] is None  # not shown as a failure


def test_new_install_removed_again_after_a_late_cancel(started):
    install, run = started
    install.cancel()
    finish(run, 0, "cancelled", demo="cancelled")
    status = install.status()
    assert status["cancelled"] and not status["kept"]


def test_update_already_in_place_stays(started):
    install, run = started
    install._updating = True
    install.cancel()
    finish(run, 0, "done", demo="cancelled")
    status = install.status()
    assert status["cancelled"] and status["kept"] and status["ok"] and status["updating"]


def test_without_cancel_a_failure_is_still_a_failure(started):
    install, run = started
    finish(run, 4, "files")
    status = install.status()
    assert not status.get("cancelled") and status["error"]


# ---------------------------------------------------------------------- the demo database setup

@pytest.fixture
def demo(tmp_path, monkeypatch):
    from launcher import demo_db
    root = tmp_path / "demo-db"
    monkeypatch.setattr(demo_db, "root_dir", lambda: root)
    monkeypatch.setattr(demo_db, "stop", lambda: None)
    progress_file = tmp_path / "progress.json.demo"
    monkeypatch.setattr(sys, "argv", ["TuningBuddy.exe", "--setup-demo", "--progress-file", str(progress_file)])
    yield demo_db, root, progress_file
    import logging
    for handler in list(logging.getLogger().handlers):
        if isinstance(handler, logging.FileHandler):
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_demo_setup_stops_between_steps_and_removes_what_it_loaded(demo, monkeypatch):
    demo_db, root, progress_file = demo
    steps = []

    def fake_setup(progress):
        root.mkdir(parents=True, exist_ok=True)
        demo_db.save_state({"seeded": False})
        (root / "data").mkdir()
        progress("Creating the demo database server")
        steps.append(1)
        Path(f"{progress_file}.cancel").write_text("", encoding="utf-8")
        progress("Loading large_orders · 1M rows")
        steps.append(2)

    monkeypatch.setattr(demo_db, "setup", fake_setup)
    assert demo_db.run_setup_cli() == demo_db.EXIT_CANCELLED
    assert steps == [1]
    assert not root.exists()
    assert progress_file.read_text(encoding="utf-8").splitlines() == ["Creating the demo database server"]


def test_demo_setup_cancel_keeps_a_finished_demo_database(demo, monkeypatch):
    demo_db, root, progress_file = demo

    def fake_setup(progress):
        root.mkdir(parents=True, exist_ok=True)
        demo_db.save_state({"seeded": True})
        Path(f"{progress_file}.cancel").write_text("", encoding="utf-8")
        progress("The demo database is already set up.")

    monkeypatch.setattr(demo_db, "setup", fake_setup)
    assert demo_db.run_setup_cli() == demo_db.EXIT_CANCELLED
    assert (root / "demo.json").exists()
