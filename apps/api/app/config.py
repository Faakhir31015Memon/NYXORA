"""Runtime configuration, loaded from .env and environment."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_TOKEN_FILE = Path("data") / "api.token"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    api_host: str = Field("127.0.0.1", alias="NYXORA_API_HOST")
    api_port: int = Field(8000, alias="NYXORA_API_PORT")
    api_token: str = Field("", alias="NYXORA_API_TOKEN")
    cors_origins: str = Field(
        "http://localhost:5173,tauri://localhost", alias="NYXORA_CORS_ORIGINS"
    )

    db_path: Path = Field(Path("./data/nyxora.db"), alias="NYXORA_DB_PATH")

    workspace_root: Path = Field(Path("./workspace"), alias="NYXORA_WORKSPACE_ROOT")
    undo_retention_days: int = Field(7, alias="NYXORA_UNDO_RETENTION_DAYS")

    ollama_base_url: str = Field("http://127.0.0.1:11434", alias="OLLAMA_BASE_URL")
    ollama_default_model: str = Field("qwen2.5:3b", alias="OLLAMA_DEFAULT_MODEL")
    ollama_timeout_s: float = Field(120.0, alias="OLLAMA_TIMEOUT_S")

    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    openai_base_url: str = Field("https://api.openai.com/v1", alias="OPENAI_BASE_URL")

    privacy_level: str = Field("local_only", alias="PRIVACY_LEVEL")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    max_steps_per_run: int = Field(12, alias="MAX_STEPS_PER_RUN")
    max_upload_size_mb: int = Field(50, alias="MAX_UPLOAD_SIZE_MB")
    approval_expiry_s: int = Field(300, alias="APPROVAL_EXPIRY_S")
    rate_limit_per_min: int = Field(60, alias="RATE_LIMIT_PER_MIN")

    @field_validator("privacy_level")
    @classmethod
    def _validate_privacy(cls, v: str) -> str:
        allowed = {"local_only", "allow_external_with_consent", "allow_external"}
        if v not in allowed:
            raise ValueError(f"PRIVACY_LEVEL must be one of {sorted(allowed)}")
        return v

    @property
    def external_models_allowed(self) -> bool:
        return self.privacy_level != "local_only" and bool(self.openai_api_key)

    def resolved_api_token(self) -> str:
        """Per-install random token, generated on first run and reused after."""
        if self.api_token:
            return self.api_token
        if _TOKEN_FILE.exists():
            stored = _TOKEN_FILE.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        generated = secrets.token_urlsafe(32)
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(generated, encoding="utf-8")
        return generated

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
