"""Who may call the service, and where a saved API key may be sent."""
from .conftest import create_provider


# ---------------------------------------------------------------------- ServiceGuard

def test_open_when_the_env_is_unset(client):
    assert client.get("/providers").status_code == 200  # the Docker stack: unchanged


def test_token_required_when_set(client, monkeypatch):
    monkeypatch.setenv("TB_INTERNAL_TOKEN", "s3cret-token")
    assert client.get("/providers").status_code == 401
    assert client.get("/providers", headers={"X-Tuning-Buddy-Token": "wrong"}).status_code == 401
    assert client.get("/providers", headers={"X-Tuning-Buddy-Token": "s3cret-token"}).status_code == 200


def test_health_stays_open_for_the_launcher(client, monkeypatch):
    monkeypatch.setenv("TB_INTERNAL_TOKEN", "s3cret-token")
    assert client.get("/health").status_code == 200


def test_foreign_host_refused(client, monkeypatch):
    monkeypatch.setenv("SERVICE_ALLOWED_HOSTS", "127.0.0.1,localhost")
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/health", headers={"Host": "evil.example:58078"}).status_code == 400
    assert client.get("/health", headers={"Host": "127.0.0.1:58078"}).status_code == 200
    assert client.get("/health", headers={"Host": "localhost"}).status_code == 200


# ---------------------------------------------------------------------- the saved key stays put

def test_saved_key_is_not_tested_against_another_address(client, fake_providers):
    provider = create_provider(client, provider_type="openai_compatible", base_url="https://api.deepseek.com")
    response = client.post("/providers/test", json={
        "provider_id": provider["id"], "provider_type": "openai_compatible",
        "base_url": "https://attacker.example/v1", "model": "good",
    })
    assert response.status_code == 422
    assert "API key again" in response.json()["detail"]


def test_saved_key_is_not_tested_against_another_type(client, fake_providers):
    provider = create_provider(client)  # anthropic
    response = client.post("/providers/test", json={
        "provider_id": provider["id"], "provider_type": "openai_compatible",
        "base_url": "https://api.anthropic.com", "model": "good",
    })
    assert response.status_code == 422


def test_saved_key_still_tests_its_own_address(client, fake_providers):
    provider = create_provider(client, provider_type="openai_compatible", base_url="https://api.deepseek.com/v1")
    response = client.post("/providers/test", json={
        "provider_id": provider["id"], "provider_type": "openai_compatible",
        "base_url": "https://api.deepseek.com", "model": "good",  # same host, other path
    })
    assert response.status_code == 200, response.text


def test_a_new_key_may_go_anywhere(client, fake_providers):
    provider = create_provider(client, provider_type="openai_compatible", base_url="https://api.deepseek.com")
    response = client.post("/providers/test", json={
        "provider_id": provider["id"], "provider_type": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1", "model": "good", "api_key": "gsk-new-key",
    })
    assert response.status_code == 200, response.text


def test_changing_the_host_needs_the_key_again(client):
    provider = create_provider(client, provider_type="openai_compatible", base_url="https://api.deepseek.com")
    response = client.put(f"/providers/{provider['id']}", json={"base_url": "https://attacker.example/v1"})
    assert response.status_code == 422
    unchanged = client.get(f"/providers/{provider['id']}").json()
    assert unchanged["base_url"] == "https://api.deepseek.com"


def test_changing_the_host_with_a_new_key_is_fine(client):
    provider = create_provider(client, provider_type="openai_compatible", base_url="https://api.deepseek.com")
    response = client.put(f"/providers/{provider['id']}",
                          json={"base_url": "https://api.groq.com/openai/v1", "api_key": "gsk-new-key"})
    assert response.status_code == 200, response.text


def test_same_host_edits_keep_the_saved_key(client):
    provider = create_provider(client, provider_type="openai_compatible", base_url="https://api.deepseek.com")
    response = client.put(f"/providers/{provider['id']}",
                          json={"base_url": "https://api.deepseek.com/v1", "name": "DeepSeek", "model": "deepseek-chat"})
    assert response.status_code == 200, response.text
    assert response.json()["has_api_key"]


# ---------------------------------------------------------------------- no API key over plain http

def test_key_over_http_to_another_computer_refused(client):
    response = client.post("/providers", json={"name": "x", "provider_type": "openai_compatible", "model": "m",
                                               "api_key": "sk-1", "base_url": "http://api.example.com/v1"})
    assert response.status_code == 422
    assert "https://" in response.json()["detail"]


def test_local_http_and_keyless_lan_http_are_fine(client):
    for base_url, key in (("http://localhost:11434/v1", "x"), ("http://127.0.0.1:1234/v1", "x"),
                          ("http://192.168.1.20:11434/v1", None)):
        payload = {"name": "x", "provider_type": "openai_compatible", "model": "m", "base_url": base_url}
        if key:
            payload["api_key"] = key
        response = client.post("/providers", json=payload)
        assert response.status_code == 201, (base_url, response.text)
