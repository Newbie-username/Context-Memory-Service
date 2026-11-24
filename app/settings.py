from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.contracts import Contract, Identifier, TaskType


def internal_url(value: str) -> str:
    url = urlsplit(value)
    if (
        not url.hostname
        or url.username is not None
        or url.fragment
        or url.scheme not in ("http", "https")
        or (
            url.scheme == "http"
            and url.hostname not in ("127.0.0.1", "::1", "localhost")
        )
    ):
        raise ValueError("dependency URL must use HTTPS or loopback HTTP")
    return value


class EmbeddingProfile(Contract):
    url: str
    model_version: Identifier
    dimensions: int = Field(ge=1, le=2000)
    token: SecretStr | None = None
    timeout_seconds: float = Field(default=1.5, gt=0, le=30)

    validate_url = field_validator("url")(internal_url)


class ModelProfile(Contract):
    model_id: Identifier
    window: int = Field(gt=0)
    max_output: int = Field(gt=0)
    safety_buffer: int = Field(ge=0)
    dynamic_cap: int = Field(ge=0)
    tokenizer_file: Path | None = None
    fixed_tools: list[dict] = Field(default_factory=list, max_length=32)
    knowledge_weight: int = Field(default=45, gt=0)
    history_weight: int = Field(default=15, gt=0)
    memory_weight: int = Field(default=20, gt=0)

    @model_validator(mode="after")
    def validate_window(self):
        if self.max_output + self.safety_buffer >= self.window:
            raise ValueError("model window must leave room for input")
        return self


class Application(Contract):
    tenants: frozenset[Identifier] = Field(min_length=1)
    routes: frozenset[TaskType] = Field(min_length=1)
    model: ModelProfile
    memory_enabled: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTEXT_", hide_input_in_errors=True)

    database_url: SecretStr
    jwt_issuer: Annotated[str, Field(min_length=1)]
    jwt_audience: Annotated[str, Field(min_length=1)]
    jwt_public_key_file: Path
    applications: dict[Identifier, Application] = Field(min_length=1)
    rag_url: str | None = None
    rag_token: SecretStr | None = None
    rag_timeout_seconds: float = Field(default=1.5, gt=0, le=30)
    otlp_endpoint: str | None = None
    embedding: EmbeddingProfile | None = None
    redis_url: SecretStr | None = None

    @field_validator("rag_url")
    @classmethod
    def validate_rag_url(cls, value: str | None) -> str | None:
        return internal_url(value) if value is not None else None
