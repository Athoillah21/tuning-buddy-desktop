import os
import tempfile

from cryptography.fernet import Fernet

# Must be set before the app modules are imported
os.environ["AI_DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'test_ai.db')}"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import service  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import ProviderError  # noqa: E402


class FakeAdapter:
    """Returns (or raises) whatever behaviour is registered for its model name."""

    def __init__(self, behavior):
        self.behavior = behavior

    def complete(self, system, prompt, max_tokens=4096):
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior


VALID_RECOMMENDATIONS = """```json
[{"type": "index", "description": "Add index", "optimized_query": "SELECT 1",
  "suggested_indexes": ["CREATE INDEX idx ON t(a)"], "expected_improvement": "high", "explanation": "x"}]
```"""

BEHAVIORS = {
    "good": '{"status": "ok"}',
    "bad-key": ProviderError("Error code: 401 - invalid x-api-key", kind="auth"),
    "limited": ProviderError("Error code: 429 - rate limited", kind="rate_limit"),
    "chatty": "Sure, everything is fine!",
    "recs": VALID_RECOMMENDATIONS,
}


@pytest.fixture
def fake_providers(monkeypatch):
    def fake_make_adapter(provider_type, api_key, model, base_url, *, timeout, max_retries):
        return FakeAdapter(BEHAVIORS[model])

    monkeypatch.setattr(service, "make_adapter", fake_make_adapter)
    return BEHAVIORS


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:
        yield test_client


def create_provider(client, **overrides):
    payload = {
        "name": "Test provider",
        "provider_type": "anthropic",
        "model": "good",
        "api_key": "sk-test-1234567890",
        "priority": 100,
    }
    payload.update(overrides)
    response = client.post("/providers", json=payload)
    assert response.status_code == 201, response.text
    return response.json()
