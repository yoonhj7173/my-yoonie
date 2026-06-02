from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from harness.providers.base import BaseProvider, ProviderResult

log = logging.getLogger(__name__)

_API_URL = "https://openrouter.ai/api/v1/chat/completions"
_API_KEY_ENV = "OPENROUTER_API_KEY"
_TIMEOUT_SECONDS = 120


class OpenRouterProvider(BaseProvider):
    """
    Calls the OpenRouter API (OpenAI-compatible chat completions endpoint).

    Requires OPENROUTER_API_KEY in the environment.
    Uses urllib.request (stdlib) — no extra dependencies.
    Supports streaming via on_token callback.
    Never logs the API key.
    """

    def generate(
        self,
        prompt_package: dict,
        model_config: dict,
        on_token: Callable[[str], None] | None = None,
        history: list[dict] | None = None,
    ) -> ProviderResult:
        api_key = os.environ.get(_API_KEY_ENV)
        model = model_config.get("model", "")
        provider_name = "openrouter"

        if not api_key:
            log.error(
                "OpenRouter: %s is not set. Set it in your environment or .env file.",
                _API_KEY_ENV,
            )
            return ProviderResult(
                text="",
                provider=provider_name,
                model=model,
                status="error",
                error=f"{_API_KEY_ENV} is not set",
            )

        temperature = float(model_config.get("temperature", 0.2))
        messages = [
            {"role": "system", "content": prompt_package["system_prompt"]},
            *(history or []),
            {"role": "user", "content": prompt_package["user_message"]},
        ]

        streaming = on_token is not None
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": streaming,
        }
        if streaming:
            # Request usage in the final streaming chunk so we can log tokens/cost.
            payload["stream_options"] = {"include_usage": True}
        body = json.dumps(payload).encode()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/harness",
            "X-Title": "AI Harness",
        }

        req = urllib.request.Request(
            _API_URL, data=body, headers=headers, method="POST"
        )

        log.info(
            "OpenRouter call | provider=%s model=%s tier=%s stream=%s",
            provider_name,
            model,
            model_config.get("tier", ""),
            streaming,
        )
        start = time.monotonic()

        try:
            if streaming:
                return self._generate_streaming(req, model, provider_name, start, on_token)
            else:
                return self._generate_batch(req, model, provider_name, start)
        except urllib.error.HTTPError as exc:
            elapsed = time.monotonic() - start
            error_body = exc.read().decode() if exc.fp else ""
            log.error("OpenRouter HTTP %d after %.2fs | %s", exc.code, elapsed, error_body[:300])
            return ProviderResult(
                text="", provider=provider_name, model=model, status="error",
                error=f"HTTP {exc.code}: {error_body[:300]}",
            )
        except urllib.error.URLError as exc:
            elapsed = time.monotonic() - start
            log.error("OpenRouter network error after %.2fs | %s", elapsed, exc.reason)
            return ProviderResult(
                text="", provider=provider_name, model=model, status="error",
                error=f"Network error: {exc.reason}",
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.error("OpenRouter unexpected error after %.2fs | %s", elapsed, exc)
            return ProviderResult(
                text="", provider=provider_name, model=model, status="error",
                error=str(exc),
            )

    def _generate_batch(
        self, req: urllib.request.Request, model: str, provider_name: str, start: float
    ) -> ProviderResult:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode()

        elapsed = time.monotonic() - start
        data = json.loads(raw)

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            log.error("OpenRouter: unexpected response shape | %s", exc)
            return ProviderResult(
                text="", provider=provider_name, model=model, status="error",
                error=f"Unexpected response shape: {exc}",
            )

        usage = data.get("usage")
        log.info("OpenRouter success | model=%s latency=%.2fs usage=%s", model, elapsed, usage)
        return ProviderResult(text=text, provider=provider_name, model=model, status="success", usage=usage)

    def _generate_streaming(
        self,
        req: urllib.request.Request,
        model: str,
        provider_name: str,
        start: float,
        on_token: Callable[[str], None],
    ) -> ProviderResult:
        full_text = ""
        usage: dict | None = None

        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").rstrip("\n\r")
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Capture usage from the last chunk (some providers send it here)
                if chunk.get("usage"):
                    usage = chunk["usage"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                token = delta.get("content") or ""
                if token:
                    on_token(token)
                    full_text += token

        elapsed = time.monotonic() - start
        log.info("OpenRouter stream done | model=%s latency=%.2fs", model, elapsed)
        return ProviderResult(
            text=full_text, provider=provider_name, model=model, status="success", usage=usage
        )
