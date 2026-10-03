from tests.conftest import create_provider


def test_api_key_is_masked_and_never_returned(client):
    provider = create_provider(client, api_key="sk-ant-secret-abcd")
    assert provider["api_key_masked"] == "sk-…abcd"
    assert "sk-ant-secret-abcd" not in str(client.get("/providers").json())


def test_api_key_required_for_anthropic(client):
    response = client.post("/providers", json={"name": "x", "provider_type": "anthropic", "model": "m"})
    assert response.status_code == 422


def test_base_url_required_for_openai_compatible(client):
    response = client.post("/providers", json={"name": "x", "provider_type": "openai_compatible", "model": "m"})
    assert response.status_code == 422


def test_not_ready_without_providers(client):
    assert client.get("/status").json()["ready"] is False


def test_healthy_check_makes_app_ready(client, fake_providers):
    provider = create_provider(client, model="good")
    assert client.get("/status").json()["ready"] is False

    body = client.post(f"/providers/{provider['id']}/check").json()
    assert body["result"]["healthy"] is True
    assert body["provider"]["last_check_status"] == "healthy"
    assert body["ready"] is True


def test_invalid_key_is_unhealthy(client, fake_providers):
    provider = create_provider(client, model="bad-key")
    body = client.post(f"/providers/{provider['id']}/check").json()
    assert body["result"]["healthy"] is False
    assert body["result"]["kind"] == "auth"
    assert "Invalid API key" in body["result"]["message"]
    assert body["ready"] is False


def test_non_json_reply_is_unhealthy(client, fake_providers):
    provider = create_provider(client, model="chatty")
    body = client.post(f"/providers/{provider['id']}/check").json()
    assert body["result"]["healthy"] is False
    assert body["result"]["kind"] == "invalid_output"


def test_disabled_provider_does_not_count_as_ready(client, fake_providers):
    provider = create_provider(client, model="good")
    client.post(f"/providers/{provider['id']}/check")
    client.put(f"/providers/{provider['id']}", json={"enabled": False})
    assert client.get("/status").json()["ready"] is False


def test_config_change_resets_health(client, fake_providers):
    provider = create_provider(client, model="good")
    client.post(f"/providers/{provider['id']}/check")
    updated = client.put(f"/providers/{provider['id']}", json={"model": "chatty"}).json()
    assert updated["last_check_status"] == "unknown"


def test_blank_key_on_update_keeps_existing_key(client):
    provider = create_provider(client, api_key="sk-keep-me-1234")
    updated = client.put(f"/providers/{provider['id']}", json={"api_key": "", "name": "Renamed"}).json()
    assert updated["api_key_masked"] == "sk-…1234"
    assert updated["name"] == "Renamed"


def test_test_endpoint_checks_unsaved_config(client, fake_providers):
    body = client.post("/providers/test", json={
        "provider_type": "openai_compatible", "model": "good", "base_url": "http://ollama:11434/v1",
    }).json()
    assert body["healthy"] is True


def test_check_all(client, fake_providers):
    create_provider(client, name="A", model="good")
    create_provider(client, name="B", model="bad-key")
    body = client.post("/providers/check-all").json()
    statuses = {r["provider"]["name"]: r["result"]["healthy"] for r in body["results"]}
    assert statuses == {"A": True, "B": False}
    assert body["ready"] is True


def test_recommendations_fall_back_and_mark_failed_provider_unhealthy(client, fake_providers):
    first = create_provider(client, name="Primary", model="limited", priority=1)
    create_provider(client, name="Backup", model="recs", priority=2)

    response = client.post("/recommendations", json={"query": "SELECT 1", "plan": {}, "execution_time": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider_info"]["provider_name"] == "Backup"
    assert body["recommendations"][0]["suggested_indexes"] == ["CREATE INDEX idx ON t(a)"]

    primary = client.get(f"/providers/{first['id']}").json()
    assert primary["last_check_status"] == "unhealthy"
    assert "rate limited" in primary["last_check_message"].lower()


def test_recommendations_fail_when_all_providers_fail(client, fake_providers):
    create_provider(client, model="limited")
    response = client.post("/recommendations", json={"query": "SELECT 1"})
    assert response.status_code == 503
    assert "All AI providers failed" in response.json()["detail"]
