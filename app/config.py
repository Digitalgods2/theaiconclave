"""Configuration loader. Reads config.yaml and applies environment overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787


class DatabaseConfig(BaseModel):
    path: str = "data/switchboard.db"


class LoggingConfig(BaseModel):
    level: str = "info"
    retain_days: Optional[int] = None
    audit_to_file: bool = False


class DefaultsConfig(BaseModel):
    mode: str = "resolve"
    primary_agent: str = "codex"
    consultant: str = "claude-code"
    max_rounds: int = 50               # backstop in resolve mode
    timeout_seconds: int = 180         # per agent call
    max_seconds: int = 600             # total task time ceiling (resolve mode)
    task_type: str = "general_consultation"


class PermissionsConfig(BaseModel):
    can_read_files: bool = True
    can_write_files: bool = False
    can_run_commands: bool = False
    can_access_network: bool = False
    can_install_packages: bool = False
    can_apply_patches: bool = False
    can_read_env_files: bool = False
    can_read_secrets: bool = False


class ApprovalRequiredConfig(BaseModel):
    patches: bool = True
    commands: bool = True
    package_installs: bool = True


class OrchestrationConfig(BaseModel):
    loop_detection_threshold: float = 0.8
    max_context_bytes: int = 524288
    worker_poll_interval_seconds: int = 2


class RetentionConfig(BaseModel):
    """DB retention per decision 0003 — operational trigger + tier-based selection."""
    enabled: bool = True
    max_db_size_mb: int = 2048
    max_completed_tasks: int = 1000
    min_task_age_days: int = 90
    check_interval_seconds: int = 6 * 60 * 60  # 6 hours
    # Tier 2 (final_results) is "retain indefinitely until exported" by DR0003.
    # With export tracking (DR0005) now in place, this opt-in flag lets the
    # retention pass also drop final_results rows for tasks that have been
    # exported to disk — the markdown export is the long-term archive.
    # Off by default; turn on once you trust your export workflow.
    trim_tier2_after_export: bool = False


class DashboardConfig(BaseModel):
    enabled: bool = True
    bind_to_api_port: bool = True


class AgentConfig(BaseModel):
    enabled: bool = False
    command: str = ""
    args: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    # OpenRouter slug for pricing lookup. Each frontier CLI represents one
    # declared model in the conclave — set this to the slug OpenRouter uses
    # for that exact model (e.g. "anthropic/claude-sonnet-4.6") so the
    # Pricing view shows accurate $/M rates when the seat is in API mode.
    model_slug: Optional[str] = None
    endpoint: Optional[str] = None
    supported_modes: list[str] = Field(default_factory=list)
    supported_task_types: list[str] = Field(default_factory=list)
    timeout_seconds: int = 180


class OpenRouterModel(BaseModel):
    """One model exposed as a council seat via OpenRouter (pay-per-token gateway).

    `name` is the friendly council/checkbox name (e.g. "deepseek"); `model_slug`
    is the OpenRouter model id (e.g. "deepseek/deepseek-chat").
    """
    name: str
    model_slug: str
    max_context_chars: int = 400_000
    # DR0015: when True, the adapter offers read_file / list_dir / glob tools
    # to the model instead of inlining the full sandbox into the prompt. Default
    # off per-seat — flip to True once you've verified the specific model
    # implements OpenAI-style tool calls cleanly on real tasks.
    tool_loop: bool = False


class OpenRouterConfig(BaseModel):
    """Pluggable OpenRouter-backed council seats — pay-per-token, no subscription.

    Auth is via the OPENROUTER_API_KEY env var, or the database-stored key set
    through the dashboard's Settings → API Keys panel (env wins). If neither,
    the seats register but report unavailable. Model slugs change as the catalog
    evolves; verify against https://openrouter.ai/models.

    data_collection: "deny" (default) sends provider.data_collection=deny so
    OpenRouter won't route through providers that retain/train on the prompt —
    appropriate for code review. Set "allow" to opt back in.
    """
    enabled: bool = True
    endpoint: str = "https://openrouter.ai/api/v1"
    data_collection: str = "deny"
    models: list[OpenRouterModel] = Field(default_factory=list)


class Config(BaseModel):
    protocol_version: str = "1.0"
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    approval_required: ApprovalRequiredConfig = Field(default_factory=ApprovalRequiredConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML; fall back to config.example.yaml or built-in defaults."""
    cfg_path = Path(path or os.environ.get("SWITCHBOARD_CONFIG", "config.yaml"))
    if not cfg_path.exists():
        example = Path("config.example.yaml")
        if example.exists():
            cfg_path = example
        else:
            return Config()

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)
