"""Who may call the service (security.py): off by default, on when the launcher sets the env."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_token_required_when_set(monkeypatch):
    monkeypatch.setenv("TB_INTERNAL_TOKEN", "s3cret-token")
    assert client.post("/optimize", json={}).status_code == 401
    client.headers["X-Tuning-Buddy-Token"] = "s3cret-token"
    try:
        assert client.post("/optimize", json={}).status_code != 401  # past the guard (the empty body is then refused: 422)
    finally:
        del client.headers["X-Tuning-Buddy-Token"]


def test_health_open_and_foreign_host_refused(monkeypatch):
    monkeypatch.setenv("TB_INTERNAL_TOKEN", "s3cret-token")
    monkeypatch.setenv("SERVICE_ALLOWED_HOSTS", "127.0.0.1,localhost")
    assert client.get("/health", headers={"Host": "127.0.0.1:9000"}).status_code == 200
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_unchanged_without_the_env():
    assert client.post("/optimize", json={}).status_code == 422  # reaches the endpoint, as in the Docker stack


def test_analyzer_calls_the_ai_service_with_the_key(monkeypatch):
    """The analyzer asks the AI service for recommendations directly; it must carry the key too."""
    import httpx
    from app import ai_client

    sent = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        sent["headers"] = headers or {}
        return httpx.Response(401 if not headers else 200, json={"recommendations": []},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(ai_client.httpx, "post", fake_post)
    monkeypatch.setenv("TB_INTERNAL_TOKEN", "s3cret-token")
    ai_client.AIServiceClient(base_url="http://127.0.0.1:1")._post("/recommendations", {})
    assert sent["headers"] == {"X-Tuning-Buddy-Token": "s3cret-token"}


def test_a_refused_call_explains_what_to_do(monkeypatch):
    import httpx
    import pytest
    from app import ai_client

    monkeypatch.setattr(ai_client.httpx, "post", lambda url, **kw: httpx.Response(
        401, json={"detail": "x"}, request=httpx.Request("POST", url)))
    with pytest.raises(ai_client.AIClientError, match="open it again"):
        ai_client.AIServiceClient(base_url="http://127.0.0.1:1")._post("/recommendations", {})
