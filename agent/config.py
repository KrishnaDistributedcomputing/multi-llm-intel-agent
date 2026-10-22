"""Validated settings and the shared agent/gateway model allowlist."""

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_MODEL = "qwen3:4b"
LOCAL_MODELS = frozenset(
    {LOCAL_MODEL, "qwen3:1.7b", "llama3.2:3b", "phi4-mini:3.8b", "llama3.2:1b"}
)
CONTEXT_TOKENS = 4096
OUTPUT_TOKENS = 512
MODEL_LOAD_SECONDS = 300
LOCAL_REQUEST_SECONDS = 900
AGENT_REQUEST_TIMEOUT_SECONDS = MODEL_LOAD_SECONDS + LOCAL_REQUEST_SECONDS + 10
DEVELOPMENT_NOTICE = (
    "Development only: public or synthetic data. Not authorized for federal nonpublic information."
)
REVIEW_NOTICE = (
    "Federal review hold: all processing is blocked pending agency-approved identity, "
    "authorization, and deployment approval. Do not submit nonpublic information. "
    "CUI and classified information are out of scope."
)


def normalize_digest(value: str) -> str:
    if value and not re.fullmatch(r"(sha256:)?[0-9a-f]{64}", value):
        raise ValueError("Model approval must be a full sha256 digest")
    return "sha256:" + value.removeprefix("sha256:") if value else ""


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    litellm_master_key: SecretStr = Field(min_length=32)
    allow_cloud: bool = False
    deployment_profile: Literal["development", "federal-review"] = "development"
    local_model_digest: str = ""
    catalog_path: Path = Path("models.yaml")
    gateway_url: str = "http://gateway:4000"
    ollama_url: str = "http://ollama:11434"
    verify_interval_seconds: int = Field(default=300, ge=30, le=3600)

    @model_validator(mode="after")
    def validate_digest(self) -> "GatewaySettings":
        self.local_model_digest = normalize_digest(self.local_model_digest)
        if self.deployment_profile == "federal-review" and self.allow_cloud:
            raise ValueError("Cloud processing is forbidden in the federal-review profile")
        return self


class Settings(GatewaySettings):
    agent_api_key: SecretStr = Field(min_length=32)
    usage_db_path: Path = Path("data/usage.sqlite3")
    temporal_enabled: bool = False
    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "document-processing"
    document_data_path: Path = Path("data/documents")
    document_agent_url: str = "http://agent:8000"

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        if self.agent_api_key == self.litellm_master_key:
            raise ValueError("Agent and gateway secrets must be different")
        if self.deployment_profile == "federal-review" and self.temporal_enabled:
            raise ValueError("Temporal processing is forbidden in the federal-review profile")
        return self


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["local", "cloud"]
    model: str
    enabled: bool = False
    key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*_API_KEY$")
    api_base: str | None = None
    api_version: str | None = None
    approved_digest: str = ""

    @property
    def ollama_model(self) -> str:
        return self.model.removeprefix("ollama_chat/")

    @model_validator(mode="after")
    def validate_route(self) -> "Route":
        if self.kind == "local":
            if (
                not self.model.startswith("ollama_chat/")
                or self.ollama_model not in LOCAL_MODELS
                or self.api_base
                or self.key_env
            ):
                raise ValueError("Only allowlisted local Ollama models are allowed")
            self.approved_digest = normalize_digest(self.approved_digest)
        else:
            if self.approved_digest:
                raise ValueError("Digest approvals apply only to local models")
            if not self.key_env or not self.model.startswith(
                ("openai/", "anthropic/", "gemini/", "azure/")
            ):
                raise ValueError("Cloud routes require an explicit provider and credential")
            if self.api_base:
                url = urlparse(self.api_base)
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise ValueError(
                        "External endpoints must use HTTPS without embedded credentials"
                    )
        if "*" in self.model:
            raise ValueError("Wildcard models are forbidden")
        return self


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[Route] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_models(self) -> "Catalog":
        if len({route.id for route in self.models}) != len(self.models):
            raise ValueError("Model IDs must be unique")
        local = [route for route in self.models if route.kind == "local"]
        if not any(route.enabled for route in local):
            raise ValueError("At least one enabled local model is required")
        if len({route.model for route in local}) != len(local):
            raise ValueError("Local model routes must not be duplicated")
        return self


def load_catalog(path: Path) -> Catalog:
    return Catalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
