"""Application settings. Secrets stay outside generated projects."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT / ".env", ROOT / ".env.local"), extra="ignore")
    database_url: str = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:55432/forgeflow"
    secret_key: SecretStr = SecretStr("")
    workspace_root: Path = ROOT / "workspace"
    runtime_root: Path = ROOT / ".runtime"
    llm_provider: str = "openrouter"
    llm_model: str = "openrouter/free"
    llm_fallback_models: str = ""
    openrouter_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"),
    )
    openrouter_backup_api_key: SecretStr = SecretStr("")
    llm_api_key: SecretStr = SecretStr("")
    llm_backup_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout: int = 180
    llm_max_tokens: int = Field(default=8000, ge=-1)
    llm_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    llm_send_temperature: bool = True
    llm_reasoning_effort: str = ""
    gpt_api_key: SecretStr = SecretStr("")
    gpt_enabled: bool = False
    gpt_model: str = "gpt-6-astra"
    gpt_base_url: str = "https://api.modelbridge.tech/openai/v1"
    gpt_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_completion_tokens"
    gpt_send_temperature: bool = False
    gpt_reasoning_effort: str = "low"
    deepseek_thinking: bool = False
    llm_retries: int = 1
    llm_min_interval: float = 3
    max_iterations: int = Field(default=3, ge=-1)
    max_agent_steps: int = Field(default=6, ge=-1)
    # -1 disables a model budget; zero still blocks calls.
    max_model_calls_per_run: int = Field(default=12, ge=-1)
    max_tokens_per_run: int = Field(default=80000, ge=-1)
    max_run_seconds: int = Field(default=900, ge=-1)
    execution_mode: str = "docker"
    sandbox_image: str = "forgeflow-runner:local"
    command_timeout: int = 120
    demo_start_timeout: int = 40
    browser_channel: str = "msedge"
    browser_timeout: int = 120
    git_remote_url: str = ""
    github_token: SecretStr = SecretStr("")
    enable_git_push: bool = False
    cookie_secure: bool = False
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    registration_enabled: bool = True
    share_zip_path: Path | None = None
    share_download_password: SecretStr = SecretStr("")
    preview_origin: str = ""
    public_origin: str = ""

    def prepare(self) -> None:
        self.workspace_root = self.workspace_root.resolve()
        self.runtime_root = self.runtime_root.resolve()
        if self.workspace_root == ROOT or ROOT.is_relative_to(self.workspace_root):
            raise ValueError("WORKSPACE_ROOT must not contain the platform source.")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if len(self.secret_key.get_secret_value()) < 32:
            raise ValueError(
                "Set a random SECRET_KEY of at least 32 characters. Run scripts/setup.py."
            )
        if self.execution_mode not in {"docker", "local"}:
            raise ValueError("EXECUTION_MODE must be docker or local.")


settings = Settings()
