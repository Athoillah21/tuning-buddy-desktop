"""
Provider registry: metadata for the settings UI and adapter construction.
"""
from typing import Optional

from .. import config
from .base import KIND_MESSAGES, BaseProvider, ProviderError

if config.DEPLOYMENT_MODE == "desktop":
    LOCAL_LLM_PRESETS = [
        {"name": "Ollama on this computer", "base_url": "http://localhost:11434/v1", "model": "llama3.2"},
        {"name": "LM Studio on this computer", "base_url": "http://localhost:1234/v1", "model": ""},
    ]
else:
    LOCAL_LLM_PRESETS = [
        {"name": "Ollama (docker profile)", "base_url": "http://ollama:11434/v1", "model": "llama3.2"},
        {"name": "Ollama / LM Studio on host", "base_url": "http://host.docker.internal:11434/v1", "model": ""},
    ]

PROVIDER_TYPES = {
    "gemini": {
        "label": "Google Gemini",
        "color": "#4285F4",
        "default_model": "gemini-2.0-flash",
        "base_url_required": False,
        "api_key_required": True,
        "presets": [],
    },
    "openai_compatible": {
        "label": "OpenAI-compatible",
        "color": "#10A37F",
        "default_model": "",
        "base_url_required": True,
        "api_key_required": False,
        "presets": [
            {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
            {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
            {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": ""},
            {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": ""},
            *LOCAL_LLM_PRESETS,
        ],
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "color": "#D97757",
        "default_model": "claude-opus-5",
        "base_url_required": False,
        "api_key_required": True,
        "presets": [],
    },
}


def build_adapter(provider_type: str, *, api_key: str, model: str, base_url: Optional[str] = None,
                  timeout: float = 300.0, max_retries: int = 2) -> BaseProvider:
    """Create the adapter for a provider type. SDKs are imported lazily."""
    if provider_type == "gemini":
        from .gemini import GeminiProvider as adapter_cls
    elif provider_type == "openai_compatible":
        from .openai_compat import OpenAICompatibleProvider as adapter_cls
    elif provider_type == "anthropic":
        from .anthropic_provider import AnthropicProvider as adapter_cls
    else:
        raise ProviderError(f"Unknown provider type: {provider_type}", kind="config")
    return adapter_cls(api_key=api_key, model=model, base_url=base_url, timeout=timeout, max_retries=max_retries)


__all__ = ["PROVIDER_TYPES", "KIND_MESSAGES", "BaseProvider", "ProviderError", "build_adapter"]
