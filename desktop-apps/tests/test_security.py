"""Keys protected by Windows (DPAPI) and the per-run access tokens."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher import protect, runtime  # noqa: E402


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "data_dir", lambda: tmp_path)
    return tmp_path


def test_protect_round_trip():
    value = protect.protect("a secret ✓")
    assert value.startswith("dpapi:") and "a secret" not in value
    assert protect.unprotect(value) == "a secret ✓"


def test_damaged_value_is_an_error():
    value = protect.protect("x")
    with pytest.raises(protect.ProtectError):
        protect.unprotect(value[:-10] + "AAAAAAAAA=")
    with pytest.raises(protect.ProtectError):
        protect.unprotect("plain text")


def test_new_keys_are_stored_protected(data):
    keys = runtime.load_keys()
    stored = json.loads((data / "config.json").read_text(encoding="utf-8"))
    assert all(protect.is_protected(stored[name]) for name in ("secret_key", "encryption_key"))
    assert keys["encryption_key"] not in (data / "config.json").read_text(encoding="utf-8")
    assert runtime.load_keys() == keys  # stable across starts


def test_plain_keys_from_an_earlier_version_are_protected_and_kept(data):
    old = {"secret_key": "old-secret-key-value", "encryption_key": "b" * 43 + "=", "other": 1}
    (data / "config.json").write_text(json.dumps(old), encoding="utf-8")
    keys = runtime.load_keys()
    assert keys["secret_key"] == old["secret_key"] and keys["encryption_key"] == old["encryption_key"]
    text = (data / "config.json").read_text(encoding="utf-8")
    assert old["secret_key"] not in text and old["encryption_key"] not in text
    assert json.loads(text)["other"] == 1
    assert not (data / "config.json.tmp").exists()


def test_a_key_that_cannot_be_unlocked_stops_the_start(data):
    good = protect.protect("k")
    (data / "config.json").write_text(json.dumps({"secret_key": good, "encryption_key": good[:-10] + "AAAAAAAAA="}),
                                      encoding="utf-8")
    before = (data / "config.json").read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="can't be unlocked"):
        runtime.load_keys()
    assert (data / "config.json").read_text(encoding="utf-8") == before  # never replaced by new keys


def test_each_run_gets_new_access_tokens(data, monkeypatch):
    monkeypatch.setattr(runtime, "STAGE_DIR", data)  # setup_environment checks it exists when unfrozen
    for name in ("TB_INTERNAL_TOKEN", "TB_DESKTOP_ACCESS_TOKEN", "SERVICE_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    saved = dict(os.environ)
    try:
        runtime.setup_environment()
        first = (os.environ["TB_INTERNAL_TOKEN"], os.environ["TB_DESKTOP_ACCESS_TOKEN"])
        runtime.setup_environment()
        second = (os.environ["TB_INTERNAL_TOKEN"], os.environ["TB_DESKTOP_ACCESS_TOKEN"])
        assert first != second and all(len(t) >= 40 for t in first)
        assert first[0] != first[1]
        assert os.environ["SERVICE_ALLOWED_HOSTS"] == "127.0.0.1,localhost"
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_window_opens_with_the_runs_key(monkeypatch):
    from launcher import window

    class FakeServices:
        web_url = "http://127.0.0.1:5000/"

    monkeypatch.setenv("TB_DESKTOP_ACCESS_TOKEN", "a+b/c")
    assert window.window_url(FakeServices()) == "http://127.0.0.1:5000/desktop/open/?key=a%2Bb/c"
    monkeypatch.delenv("TB_DESKTOP_ACCESS_TOKEN")
    assert window.window_url(FakeServices()) == "http://127.0.0.1:5000/"


def test_setup_test_switches_are_ignored_in_a_built_setup(monkeypatch):
    from setup_ui import main

    monkeypatch.setenv("TB_SETUP_ENGINE_DIR", r"C:\somewhere\else")
    monkeypatch.setenv("TB_SETUP_DEBUG_PORT", "9222")
    assert main.dev_switch("TB_SETUP_DEBUG_PORT") == "9222"  # from source
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main.sys, "_MEIPASS", r"C:\bundle", raising=False)  # what a built exe has
    assert main.dev_switch("TB_SETUP_DEBUG_PORT") == ""
    assert main.engine_dir() == __import__("pathlib").Path(r"C:\bundle") / "engine"


# ---------------------------------------------------------------------- demo database credentials

class _FakeCursor:
    def __init__(self, log, fail):
        self.log, self.fail = log, fail

    def execute(self, statement, params=None):
        if self.fail:
            raise RuntimeError("server said no")
        self.log.append((statement, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, log, fail=False):
        self.log, self.fail, self.autocommit = log, fail, False

    def cursor(self):
        return _FakeCursor(self.log, self.fail)

    def close(self):
        pass


@pytest.fixture
def demo(tmp_path, monkeypatch):
    from launcher import demo_db
    monkeypatch.setattr(demo_db, "root_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(demo_db, "_connect", lambda port, user, password, db: calls.append((user, password)) or
                        _FakeConn(calls))
    return demo_db, tmp_path, calls


def test_old_install_gets_a_random_demo_password_and_protected_secrets(demo):
    demo_db, root, calls = demo
    demo_db.save_state({"superuser_password": "plain-super", "seeded": True, "port": 55432})
    assert demo_db.demo_password() == "demo"  # before: the public one

    demo_db._secure_credentials(55432)

    state = json.loads((root / "demo.json").read_text(encoding="utf-8"))
    assert protect.is_protected(state["superuser_password"]) and protect.is_protected(state["demo_password"])
    assert "plain-super" not in (root / "demo.json").read_text(encoding="utf-8")
    new = demo_db.demo_password()
    assert new != "demo" and len(new) >= 30
    assert calls[0] == ("postgres", "plain-super")                    # done as the superuser
    assert calls[1] == ("ALTER ROLE demo PASSWORD %s", (new,))         # the server got the same password


def test_nothing_changes_twice(demo):
    demo_db, root, calls = demo
    demo_db.save_state({"superuser_password": "plain-super", "seeded": True})
    demo_db._secure_credentials(1)
    first = demo_db.demo_password()
    calls.clear()
    demo_db._secure_credentials(1)
    assert demo_db.demo_password() == first and calls == []


def test_a_refused_change_keeps_the_old_password_working(demo, monkeypatch):
    demo_db, root, calls = demo
    monkeypatch.setattr(demo_db, "_connect", lambda *a: _FakeConn([], fail=True))
    demo_db.save_state({"superuser_password": "plain-super", "seeded": True})
    demo_db._secure_credentials(1)
    assert demo_db.demo_password() == "demo"  # still matches the server
    assert protect.is_protected(json.loads((root / "demo.json").read_text(encoding="utf-8"))["superuser_password"])
