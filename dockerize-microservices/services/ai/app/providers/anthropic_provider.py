"""
Anthropic Claude adapter (official anthropic SDK).
"""
import anthropic

from .base import BaseProvider, ProviderError, kind_for_status, with_root_cause

# Models that support server-side refusal fallbacks: if the model declines, the API
# re-runs the request on a suitable fallback model inside the same call.
SERVER_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}

# Thinking is on by default for current Claude models and counts toward max_tokens,
# so never give Claude less room than this.
MIN_MAX_TOKENS = 16000


class AnthropicProvider(BaseProvider):

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        client_kwargs = {"api_key": self.api_key, "timeout": self.timeout, "max_retries": self.max_retries}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client = anthropic.Anthropic(**client_kwargs)

        # No temperature: current Claude models reject sampling parameters
        request = {
            "model": self.model,
            "max_tokens": max(max_tokens, MIN_MAX_TOKENS),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            if self.model in SERVER_FALLBACK_MODELS:
                response = client.beta.messages.create(
                    **request,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            else:
                response = client.messages.create(**request)
        except anthropic.APITimeoutError as e:
            raise ProviderError(str(e) or "Request timed out", kind="timeout") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(with_root_cause(e, "Connection failed"), kind="connection") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(str(e), kind=kind_for_status(e.status_code)) from e

        if response.stop_reason == "refusal":
            raise ProviderError("Claude declined this request", kind="refusal")

        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise ProviderError(f"Claude returned no text (stop_reason={response.stop_reason})", kind="invalid_output")
        return text
