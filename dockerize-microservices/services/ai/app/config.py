"""
Environment-driven settings for the AI service.
"""
import os

DATABASE_URL = os.environ.get("AI_DATABASE_URL", "sqlite:///./ai.db")
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

# "docker" (compose stack) or "desktop" (Windows app on localhost); picks the local-LLM presets
DEPLOYMENT_MODE = os.environ.get("DEPLOYMENT_MODE", "docker")

# Seconds to wait for a provider during a health check (kept short so the UI stays responsive)
HEALTH_CHECK_TIMEOUT = float(os.environ.get("AI_HEALTH_CHECK_TIMEOUT", "30"))
# Seconds to wait for a provider when generating recommendations
REQUEST_TIMEOUT = float(os.environ.get("AI_REQUEST_TIMEOUT", "300"))

# Recommendations are JSON documents holding three options; 4096 tokens truncated
# them mid-string, which made the whole reply unparseable.
RECOMMENDATION_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "8192"))
