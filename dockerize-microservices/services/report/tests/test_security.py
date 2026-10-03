"""Who may call the service (security.py): off by default, on when the launcher sets the env."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_token_required_when_set(monkeypatch):
    monkeypatch.setenv("TB_INTERNAL_TOKEN", "s3cret-token")
    assert client.post("/reports/optimization", json={}).status_code == 401
    client.headers["X-Tuning-Buddy-Token"] = "s3cret-token"
    try:
        assert client.post("/reports/optimization", json={}).status_code != 401  # past the guard (the empty body is then refused: 422)
    finally:
        del client.headers["X-Tuning-Buddy-Token"]


def test_health_open_and_foreign_host_refused(monkeypatch):
    monkeypatch.setenv("TB_INTERNAL_TOKEN", "s3cret-token")
    monkeypatch.setenv("SERVICE_ALLOWED_HOSTS", "127.0.0.1,localhost")
    assert client.get("/health", headers={"Host": "127.0.0.1:9000"}).status_code == 200
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_unchanged_without_the_env():
    assert client.post("/reports/optimization", json={}).status_code == 422  # reaches the endpoint, as in the Docker stack
