"""
Adapter for any OpenAI-compatible chat completions API
(OpenAI, DeepSeek, Groq, OpenRouter, Ollama, LM Studio, ...).
"""
import openai

from .base import BaseProvider, ProviderError, kind_for_status, with_root_cause


class OpenAICompatibleProvider(BaseProvider):

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        client = openai.OpenAI(
            # Local servers such as Ollama ignore the key but the SDK requires one
            api_key=self.api_key or "not-needed",
            base_url=self.base_url or None,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=max_tokens,
            )
        except openai.APITimeoutError as e:
            raise ProviderError(str(e) or "Request timed out", kind="timeout") from e
        except openai.APIConnectionError as e:
            raise ProviderError(with_root_cause(e, "Connection failed"), kind="connection") from e
        except openai.APIStatusError as e:
            raise ProviderError(str(e), kind=kind_for_status(e.status_code)) from e

        content = response.choices[0].message.content if response.choices else None
        text = (content or "").strip()
        if not text:
            raise ProviderError("The API returned an empty response", kind="invalid_output")
        return text
