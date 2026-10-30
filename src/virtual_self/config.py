from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.is_file() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql://virtual_self:virtual_self@127.0.0.1:54329/virtual_self",
        alias="VSM_DATABASE_URL",
    )
    field_encryption_key: SecretStr | None = Field(default=None, alias="VSM_FIELD_ENCRYPTION_KEY")
    bootstrap_token: SecretStr | None = Field(default=None, alias="VSM_BOOTSTRAP_TOKEN")

    api_host: str = Field(default="127.0.0.1", alias="VSM_API_HOST")
    api_port: int = Field(default=8090, alias="VSM_API_PORT")
    log_level: str = Field(default="INFO", alias="VSM_LOG_LEVEL")
    metrics_enabled: bool = Field(default=True, alias="VSM_METRICS_ENABLED")

    worker_enabled: bool = Field(default=True, alias="VSM_WORKER_ENABLED")
    worker_poll_seconds: float = Field(default=2.0, alias="VSM_WORKER_POLL_SECONDS")

    idempotency_ttl_hours: int = Field(default=24, alias="VSM_IDEMPOTENCY_TTL_HOURS")
    job_lease_seconds: int = Field(default=60, alias="VSM_JOB_LEASE_SECONDS")

    ollama_url: str = Field(default="http://127.0.0.1:11434", alias="VSM_OLLAMA_URL")
    embed_model: str = Field(default="nomic-embed-text", alias="VSM_EMBED_MODEL")
    embed_dims: int = Field(default=768, alias="VSM_EMBED_DIMS")
    chat_model: str = Field(
        default="qwen3.6:35b-a3b",
        alias="VSM_CHAT_MODEL",
        description="Thaw first: ollama-cold thaw qwen3.6:35b-a3b (see your local model manager)",
    )
    chat_timeout_seconds: float = Field(default=120.0, alias="VSM_CHAT_TIMEOUT_SECONDS")
    llm_backend: Literal["auto", "ollama", "openai"] = Field(default="auto", alias="VSM_LLM_BACKEND")
    openai_base_url: str = Field(default="http://127.0.0.1:8181/v1", alias="VSM_OPENAI_BASE_URL")
    openai_model: str = Field(default="local", alias="VSM_OPENAI_MODEL")
    openai_api_key: SecretStr | None = Field(default=None, alias="VSM_OPENAI_API_KEY")

    require_agent_enrichment: bool = Field(default=True, alias="VSM_REQUIRE_AGENT_ENRICHMENT")
    enrich_on_import: bool = Field(default=True, alias="VSM_ENRICH_ON_IMPORT")
    enrich_thaw_cold_model: bool = Field(default=False, alias="VSM_ENRICH_THAW_COLD_MODEL")
    enrich_parallel_workers: int = Field(
        default=1,
        ge=1,
        le=32,
        alias="VSM_ENRICH_PARALLEL_WORKERS",
        description="Concurrent page enrichments (use 4–12 with OpenRouter; keep 1 for local Ollama)",
    )

    my_life_root: Path = Field(
        default=Path(
            "/data/corpus"
        ),
        alias="VSM_MY_LIFE_ROOT",
    )

    chat_memory_enabled: bool = Field(default=True, alias="VSM_CHAT_MEMORY_ENABLED")
    chat_memory_min_confidence: float = Field(default=0.85, alias="VSM_CHAT_MEMORY_MIN_CONFIDENCE")
    chat_memory_turn_window: int = Field(default=6, alias="VSM_CHAT_MEMORY_TURN_WINDOW")
    chat_memory_notify_on_save: bool = Field(default=False, alias="VSM_CHAT_MEMORY_NOTIFY_ON_SAVE")
    chat_memory_model: str = Field(
        default="qwen2.5:7b",
        alias="VSM_CHAT_MEMORY_MODEL",
        description="Small classifier for chat.memory.extract (Ollama pull; not VSM_CHAT_MODEL / :8081)",
    )
    chat_memory_llm_backend: Literal["auto", "ollama", "openai"] = Field(
        default="ollama",
        alias="VSM_CHAT_MEMORY_LLM_BACKEND",
        description="Prefer ollama so async extract does not use the main chat LLM on :8081",
    )

    persona_reflect_enabled: bool = Field(default=True, alias="VSM_PERSONA_REFLECT_ENABLED")
    persona_reflect_interval_hours: float = Field(
        default=168.0,
        alias="VSM_PERSONA_REFLECT_INTERVAL_HOURS",
        description="Weekly default; worker enqueues persona.reflect when elapsed",
    )
    persona_reflect_turn_threshold: int = Field(
        default=8,
        alias="VSM_PERSONA_REFLECT_TURN_THRESHOLD",
        description="Enqueue after this many new user chat turns since last completed reflect job",
    )
    persona_reflect_turn_window: int = Field(default=40, alias="VSM_PERSONA_REFLECT_TURN_WINDOW")
    persona_reflect_lookback_days: int = Field(default=14, alias="VSM_PERSONA_REFLECT_LOOKBACK_DAYS")
    persona_reflect_min_user_turns: int = Field(default=2, alias="VSM_PERSONA_REFLECT_MIN_USER_TURNS")
    persona_reflect_stale_days: int = Field(
        default=30,
        alias="VSM_PERSONA_REFLECT_STALE_DAYS",
        description="Auto-expire pending reflection proposals older than this",
    )
    enrich_notify_milestones: bool = Field(default=False, alias="VSM_ENRICH_NOTIFY_MILESTONES")

    archive_data_root: Path = Field(
        default=Path("/data/archive"),
        alias="VSM_ARCHIVE_DATA_ROOT",
    )
    archive_enabled: bool = Field(default=True, alias="VSM_ARCHIVE_ENABLED")
    archive_rclone_config: Path = Field(
        default=Path("/config/rclone.conf"),
        alias="VSM_ARCHIVE_RCLONE_CONFIG",
    )
    archive_rclone_remote: str = Field(default="personal-archive", alias="VSM_ARCHIVE_RCLONE_REMOTE")
    archive_takeout_path: str = Field(default="Takeout", alias="VSM_ARCHIVE_TAKEOUT_PATH")
    self_emails: str = Field(
        default="",
        alias="VSM_SELF_EMAILS",
        description="Comma-separated addresses for sent-mail authorship (optional if Gmail Sent label present)",
    )
    archive_sync_poll_seconds: float = Field(default=900.0, alias="VSM_ARCHIVE_SYNC_POLL_SECONDS")
    archive_stability_poll_seconds: float = Field(
        default=60.0, alias="VSM_ARCHIVE_STABILITY_POLL_SECONDS"
    )
    archive_min_free_gb: float = Field(default=15.0, alias="VSM_ARCHIVE_MIN_FREE_GB")
    archive_streaming: bool = Field(default=True, alias="VSM_ARCHIVE_STREAMING")
    archive_delete_zip_after: bool = Field(default=False, alias="VSM_ARCHIVE_DELETE_ZIP_AFTER")
    archive_min_confidence: float = Field(default=0.8, alias="VSM_ARCHIVE_MIN_CONFIDENCE")
    archive_llm_enabled: bool = Field(default=True, alias="VSM_ARCHIVE_LLM_ENABLED")
    archive_supervisor_enabled: bool = Field(default=True, alias="VSM_ARCHIVE_SUPERVISOR_ENABLED")

    web_gateway_url: str = Field(
        default="http://127.0.0.1:8099",
        alias="VSM_WEB_GATEWAY_URL",
        description="MCP knowledge-gateway base (host pytest :8099; containers: http://knowledge-gateway:8080)",
    )
    web_gateway_token: str | None = Field(default=None, alias="WEB_GATEWAY_MCP_TOKEN")
    web_audit_path: Path = Field(
        default=_PROJECT_ROOT / ".state" / "web_audit.jsonl",
        alias="VSM_WEB_AUDIT_PATH",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
