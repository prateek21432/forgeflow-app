"""Choose a server-configured model per run without sharing mutable settings."""

from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import SecretStr

from ..config import Settings, settings

_profile: ContextVar[str] = ContextVar("model_profile", default="default")


def available_profiles() -> list[dict[str, str]]:
    provider = "DeepSeek" if settings.llm_provider == "deepseek" else settings.llm_provider
    profiles = [{"id": "default", "label": f"{provider} · {settings.llm_model}"}]
    if settings.gpt_enabled and settings.gpt_api_key.get_secret_value():
        profiles.append({"id": "gpt", "label": f"GPT · {settings.gpt_model}"})
    return profiles


def validate_profile(name: str) -> str:
    if name not in {profile["id"] for profile in available_profiles()}:
        raise ValueError("This model is not configured on the server.")
    return name


@contextmanager
def use_profile(name: str):
    token = _profile.set(validate_profile(name))
    try:
        yield
    finally:
        _profile.reset(token)


def current_settings() -> Settings:
    if _profile.get() == "default":
        return settings
    validate_profile("gpt")
    return settings.model_copy(
        update={
            "llm_provider": "openai_compatible",
            "llm_model": settings.gpt_model,
            "llm_base_url": settings.gpt_base_url,
            "llm_api_key": settings.gpt_api_key,
            "llm_backup_api_key": SecretStr(""),
            "llm_fallback_models": "",
            "llm_token_parameter": settings.gpt_token_parameter,
            "llm_send_temperature": settings.gpt_send_temperature,
            "llm_reasoning_effort": settings.gpt_reasoning_effort,
        }
    )
