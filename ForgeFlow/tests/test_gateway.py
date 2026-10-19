import httpx
import pytest
from pydantic import SecretStr

from app.config import settings
from app.services.llm_gateway import (
    LLMGateway,
    ModelBudgetError,
    ModelError,
    begin_budget,
    budget_usage,
)


def configure(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", SecretStr("primary-test"))
    monkeypatch.setattr(settings, "openrouter_backup_api_key", SecretStr("backup-test"))
    monkeypatch.setattr(settings, "llm_api_key", SecretStr(""))
    monkeypatch.setattr(settings, "llm_backup_api_key", SecretStr(""))
    monkeypatch.setattr(settings, "llm_fallback_models", "")
    monkeypatch.setattr(settings, "llm_min_interval", 0)
    monkeypatch.setattr(settings, "llm_max_tokens", 3000)
    monkeypatch.setattr(settings, "max_model_calls_per_run", 8)
    monkeypatch.setattr(settings, "max_tokens_per_run", 40000)


@pytest.mark.asyncio
async def test_invalid_primary_uses_backup(monkeypatch):
    configure(monkeypatch)
    used = []

    async def post(self, url, **kwargs):
        key = kwargs["headers"]["Authorization"]
        used.append(key)
        data = (
            {"error": {"message": "invalid"}}
            if key.endswith("primary-test")
            else {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 5},
            }
        )
        return httpx.Response(
            401 if key.endswith("primary-test") else 200,
            json=data,
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await LLMGateway().generate("system", "prompt") == "ok"
    assert used == ["Bearer primary-test", "Bearer backup-test"]


@pytest.mark.asyncio
async def test_daily_quota_stops_without_retrying_keys(monkeypatch):
    configure(monkeypatch)
    calls = []

    async def post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(
            429,
            json={"error": {"message": "free-models-per-day limit"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(ModelError, match="quota"):
        await LLMGateway().generate("system", "prompt")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_call_budget_stops_before_network(monkeypatch):
    configure(monkeypatch)
    begin_budget()
    monkeypatch.setattr(settings, "max_model_calls_per_run", 0)
    with pytest.raises(ModelBudgetError):
        await LLMGateway().generate("system", "prompt")


@pytest.mark.asyncio
async def test_native_gemini_transport_and_token_accounting(monkeypatch):
    configure(monkeypatch)
    begin_budget()
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("primary-test"))
    monkeypatch.setattr(settings, "llm_model", "gemini-2.5-flash")
    monkeypatch.setattr(
        settings, "llm_base_url", "https://generativelanguage.googleapis.com/v1beta"
    )

    async def post(self, url, **kwargs):
        assert url.endswith("/models/gemini-2.5-flash:generateContent")
        assert kwargs["headers"]["x-goog-api-key"] == "primary-test"
        assert "Authorization" not in kwargs["headers"]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"ok":true}'}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"totalTokenCount": 42},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await LLMGateway().generate("system", "prompt") == '{"ok":true}'
    assert budget_usage() == {"calls": 1, "tokens": 42}


@pytest.mark.asyncio
async def test_streamed_completion_is_assembled_with_usage(monkeypatch):
    configure(monkeypatch)
    begin_budget()

    async def post(self, url, **kwargs):
        assert kwargs["json"]["stream"] is True
        payload = (
            'data: {"choices":[{"delta":{"content":"hel"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"total_tokens":9}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await LLMGateway().generate("system", "prompt") == "hello"
    assert budget_usage()["tokens"] == 9


def test_custom_provider_never_uses_stored_openrouter_credentials(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    assert LLMGateway().keys() == []
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("custom-test"))
    assert LLMGateway().keys() == ["custom-test"]


@pytest.mark.asyncio
async def test_configurable_completion_parameters(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(settings, "llm_token_parameter", "max_completion_tokens")
    monkeypatch.setattr(settings, "llm_send_temperature", False)
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")

    async def post(self, url, **kwargs):
        payload = kwargs["json"]
        assert "max_tokens" not in payload and "temperature" not in payload
        assert payload["max_completion_tokens"] == settings.llm_max_tokens
        assert payload["reasoning_effort"] == "low"
        assert "thinking" not in payload
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    async with httpx.AsyncClient() as client:
        await LLMGateway().send(client, "configured-model", "custom-test", "system", "prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [False, True])
async def test_deepseek_thinking_is_explicit(monkeypatch, thinking):
    configure(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_thinking", thinking)

    async def post(self, url, **kwargs):
        assert kwargs["json"]["thinking"] == {"type": "enabled" if thinking else "disabled"}
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    async with httpx.AsyncClient() as client:
        await LLMGateway().send(client, "test-model", "test-key", "system", "prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,finish,expected",
    [("partial", "length", "response limit"), ("", "stop", "no usable content")],
)
async def test_unusable_response_stops_without_spending_on_retries(
    monkeypatch, content, finish, expected
):
    configure(monkeypatch)
    begin_budget()
    monkeypatch.setattr(settings, "llm_retries", 3)
    monkeypatch.setattr(settings, "llm_fallback_models", "another-model")
    calls = []

    async def post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": finish}],
                "usage": {"total_tokens": 3005},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(ModelError, match=expected):
        await LLMGateway().generate("system", "prompt")
    assert len(calls) == 1
    assert budget_usage() == {"calls": 1, "tokens": 3005}


@pytest.mark.asyncio
async def test_unlimited_model_budget_still_tracks_usage(monkeypatch):
    configure(monkeypatch)
    begin_budget()
    for name in ("llm_max_tokens", "max_model_calls_per_run", "max_tokens_per_run"):
        monkeypatch.setattr(settings, name, -1)

    async def post(self, url, **kwargs):
        assert "max_tokens" not in kwargs["json"]
        assert "max_completion_tokens" not in kwargs["json"]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 100000},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    gateway = LLMGateway()
    for _ in range(10):
        assert await gateway.generate("system", "prompt") == "ok"
    assert budget_usage() == {"calls": 10, "tokens": 1000000}
