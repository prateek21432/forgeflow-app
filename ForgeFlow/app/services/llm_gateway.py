"""Model transport with key fallback, rate limits, strict JSON, and per-run budgets."""

import asyncio
import json
import time
from contextvars import ContextVar
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..security import redact
from .model_profiles import current_settings

T = TypeVar("T", bound=BaseModel)
_usage: ContextVar[dict | None] = ContextVar("model_usage", default=None)


def begin_budget() -> None:
    _usage.set({"calls": 0, "tokens": 0})


def budget_usage() -> dict:
    return dict(_usage.get() or {"calls": 0, "tokens": 0})


class ModelError(RuntimeError):
    pass


class ModelBudgetError(ModelError):
    pass


class LLMGateway:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_call = 0.0
        self.preferred_key = 0

    def keys(self) -> list[str]:
        settings = current_settings()
        primary = settings.llm_api_key.get_secret_value()
        backup = settings.llm_backup_api_key.get_secret_value()
        # Credentials belong to one provider. Never send a different provider's
        # stored key to a custom endpoint when its own backup is empty.
        if settings.llm_provider == "openrouter":
            primary = primary or settings.openrouter_api_key.get_secret_value()
            backup = backup or settings.openrouter_backup_api_key.get_secret_value()
        return list(dict.fromkeys(key for key in [primary, backup] if key))

    def reserve_call(self, prompt: str) -> None:
        settings = current_settings()
        usage = _usage.get()
        if usage is None:
            return
        estimated = len(prompt) // 3 + max(settings.llm_max_tokens, 0)
        if (
            settings.max_model_calls_per_run != -1
            and usage["calls"] >= settings.max_model_calls_per_run
        ):
            raise ModelBudgetError(
                "Model call limit reached. Work is saved; no more calls will be made."
            )
        if (
            settings.max_tokens_per_run != -1
            and usage["tokens"] + estimated > settings.max_tokens_per_run
        ):
            raise ModelBudgetError(
                "Token allowance reached. Work is saved; use Resume saved work for a fresh allowance."
            )
        usage["calls"] += 1

    async def generate(self, system: str, prompt: str) -> str:
        settings = current_settings()
        keys = self.keys()
        if not keys:
            raise ModelError("No model API key is configured. Update the private .env file.")
        if settings.llm_provider not in {
            "openrouter",
            "openai_compatible",
            "deepseek",
            "gemini",
            "groq",
            "ollama",
        }:
            raise ModelError("Unsupported provider. Configure an OpenAI-compatible endpoint.")
        models = [settings.llm_model] + [
            m.strip() for m in settings.llm_fallback_models.split(",") if m.strip()
        ]
        last_error = "Model request failed."
        order = list(range(len(keys)))
        if self.preferred_key in order:
            order.remove(self.preferred_key)
            order.insert(0, self.preferred_key)
        for model in models:
            for key_index in order:
                for attempt in range(settings.llm_retries):
                    self.reserve_call(system + prompt)
                    async with self.lock:
                        delay = settings.llm_min_interval - (time.monotonic() - self.last_call)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self.last_call = time.monotonic()
                    try:
                        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
                            response = await self.send(
                                client, model, keys[key_index], system, prompt
                            )
                        if response.status_code in {401, 403, 402}:
                            last_error = f"Provider rejected credential {key_index + 1} (HTTP {response.status_code})."
                            break  # Try the configured backup; never expose key values.
                        if response.status_code == 429:
                            provider_error = response.json().get("error", {})
                            message = str(provider_error.get("message", "Rate limit exceeded."))
                            quota_details = json.dumps(provider_error.get("details", [])).lower()
                            daily = any(
                                word in (message.lower() + quota_details)
                                for word in ("per-day", "daily", "perday")
                            )
                            if daily:
                                raise ModelError(
                                    "Provider daily quota is exhausted. The run stopped without further retries. "
                                    + redact(message.split("For more information")[0])[:350]
                                )
                            retry_after = response.headers.get("retry-after", "10")
                            for detail in provider_error.get("details", []):
                                if "RetryInfo" in detail.get("@type", ""):
                                    retry_after = detail.get("retryDelay", "10s").removesuffix("s")
                            seconds = int(retry_after) if retry_after.isdigit() else 10
                            if seconds > 60 or attempt + 1 == settings.llm_retries:
                                raise ModelError(
                                    "Provider rate limit reached. "
                                    + redact(message)[:600]
                                    + f" Retry-After: {retry_after} seconds."
                                )
                            await asyncio.sleep(max(seconds + 1, 1))
                            continue
                        response.raise_for_status()
                        data = response.json()
                        if settings.llm_provider == "gemini":
                            candidate = data.get("candidates", [{}])[0]
                            content = "".join(
                                part.get("text", "")
                                for part in candidate.get("content", {}).get("parts", [])
                                if not part.get("thought")
                            )
                            truncated = candidate.get("finishReason") == "MAX_TOKENS"
                            tokens = data.get("usageMetadata", {}).get("totalTokenCount")
                        else:
                            choice = data["choices"][0]
                            content = choice["message"].get("content")
                            truncated = choice.get("finish_reason") == "length"
                            tokens = data.get("usage", {}).get("total_tokens")
                        usage = _usage.get()
                        if usage is not None:
                            usage["tokens"] += int(tokens or (len(system + prompt) // 3))
                        if truncated:
                            raise ModelError(
                                "Model reached its response limit. "
                                "No partial response was applied. Request smaller file batches. "
                                "Stopped without retrying to avoid repeating the same charge."
                            )
                        if not content:
                            raise ModelError(
                                "Model returned no usable content. Check the model's thinking settings. "
                                "Stopped without retrying to avoid repeating the same charge."
                            )
                        self.preferred_key = key_index
                        return content
                    except ModelError:
                        raise
                    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                        last_error = redact(str(exc))[:1000]
                        if attempt + 1 < settings.llm_retries:
                            await asyncio.sleep(min(2 ** (attempt + 1), 8))
        raise ModelError(last_error + " Primary and configured fallback options were unsuccessful.")

    async def send(self, client: httpx.AsyncClient, model: str, key: str, system: str, prompt: str):
        settings = current_settings()
        if settings.llm_provider == "gemini":
            generation = {
                "temperature": 0.15,
                "responseMimeType": "application/json",
            }
            if settings.llm_max_tokens != -1:
                generation["maxOutputTokens"] = settings.llm_max_tokens
            if model.startswith("gemini-2.5"):
                generation["thinkingConfig"] = {"thinkingBudget": 1024}
            return await client.post(
                settings.llm_base_url.rstrip("/") + "/models/" + model + ":generateContent",
                headers={"x-goog-api-key": key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": generation,
                },
            )
        payload = {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if settings.llm_max_tokens != -1:
            payload[settings.llm_token_parameter] = settings.llm_max_tokens
        if settings.llm_send_temperature:
            payload["temperature"] = 0.15
        if settings.llm_reasoning_effort:
            payload["reasoning_effort"] = settings.llm_reasoning_effort
        if settings.llm_provider == "deepseek":
            payload["thinking"] = {"type": "enabled" if settings.deepseek_thinking else "disabled"}
        response = await client.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": "Bearer " + key,
                "X-Title": "ForgeFlow",
            },
            json=payload,
        )
        if response.status_code != 200 or "text/event-stream" not in response.headers.get(
            "content-type", ""
        ):
            return response
        # httpx consumes incoming chunks as they arrive. Its read timeout therefore
        # measures a stalled connection, not the time needed to generate a whole file.
        fragments = []
        finish_reason = None
        usage = {}
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            chunk = json.loads(raw)
            if chunk.get("error"):
                raise ModelError(redact(str(chunk["error"]))[:1000])
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices", []):
                text = choice.get("delta", {}).get("content")
                if text:
                    fragments.append(text)
                finish_reason = choice.get("finish_reason") or finish_reason
        if finish_reason is None:
            raise ValueError("Provider stream ended without a completion marker.")
        return httpx.Response(
            200,
            request=response.request,
            json={
                "choices": [
                    {"message": {"content": "".join(fragments)}, "finish_reason": finish_reason}
                ],
                "usage": usage,
            },
        )

    async def generate_structured(self, system: str, prompt: str, schema: type[T]) -> T:
        instruction = "\nReturn only a JSON object matching this schema:\n" + json.dumps(
            schema.model_json_schema()
        )
        correction = ""
        for _ in range(2):
            raw = await self.generate(system + instruction, prompt + correction)
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            try:
                return schema.model_validate_json(text)
            except ValidationError as exc:
                correction = (
                    "\nYour previous JSON was invalid. Fix these schema errors: " + str(exc)[:2500]
                )
        raise ModelError("The model returned invalid structured output twice. Stopping the run.")


gateway = LLMGateway()
