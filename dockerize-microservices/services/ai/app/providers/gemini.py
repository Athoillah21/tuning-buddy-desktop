"""
Google Gemini adapter (google-genai SDK).
"""
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .base import BaseProvider, ProviderError, kind_for_status


class GeminiProvider(BaseProvider):

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        http_options = types.HttpOptions(timeout=int(self.timeout * 1000))
        if self.base_url:
            http_options.base_url = self.base_url
        client = genai.Client(api_key=self.api_key, http_options=http_options)

        try:
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.3,
                    max_output_tokens=max_tokens,
                    # No tools are used; avoids the SDK's automatic function calling path
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
        except genai_errors.APIError as e:
            raise ProviderError(str(e), kind=kind_for_status(e.code)) from e
        except httpx.TimeoutException as e:
            raise ProviderError(str(e) or "Request timed out", kind="timeout") from e
        except httpx.HTTPError as e:
            raise ProviderError(str(e) or "Connection failed", kind="connection") from e

        text = (response.text or "").strip()
        if not text:
            raise ProviderError("Gemini returned an empty response", kind="invalid_output")
        return text
