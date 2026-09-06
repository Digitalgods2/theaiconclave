"""Configuration loader. Reads config.yaml and applies environment overrides.

Config discovery order (DR0016):

1. Explicit `path` arg passed to `load_config(path=...)`.
2. `SWITCHBOARD_CONFIG` environment variable.
3. `<user_data_root>/config.yaml` (the packaged-app default).
4. Repo-relative `./config.yaml` (dev-mode override).
5. Repo-relative `./config.example.yaml` (dev-mode fallback).
6. Built-in `Config()` defaults.

On first run in a packaged app (step 3 missing, step 4 also missing), the
packaged `config.example.yaml` is copied into `<user_data_root>/config.yaml`
once so the user has a starting config they can edit.
"""

from __future__ import annotations

import logging
import ipaddress
import os
import shutil
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger("switchboard.config")


class ConfigModel(BaseModel):
    """Strict base so misspelled configuration never disappears silently."""

    model_config = ConfigDict(extra="forbid")


class ServerConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    allow_remote: bool = False
    api_token: Optional[str] = Field(default=None, min_length=16)


class DatabaseConfig(ConfigModel):
    # `None` means "use default_db_path() at resolution time". An explicit
    # string in config.yaml still wins. See DR0016 — the old default of
    # "data/switchboard.db" was CWD-relative and broke packaged launches.
    path: Optional[str] = None


class LoggingConfig(ConfigModel):
    level: str = "info"

    @field_validator("level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"debug", "info", "warning", "warn", "error", "critical"}:
            raise ValueError("logging.level must be debug, info, warning, warn, error, or critical")
        return normalized


class OrchestrationConfig(ConfigModel):
    worker_poll_interval_seconds: int = Field(default=2, ge=1, le=3600)
    judge_agent: Optional[str] = None
    synthesis_agent: Optional[str] = None


class RetentionConfig(ConfigModel):
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


class AgentConfig(ConfigModel):
    enabled: bool = False
    # Absolute path override for the CLI binary. When set, the adapter uses it
    # in preference to `shutil.which(command)`. Useful in packaged apps where
    # GUI launches don't inherit the user's shell PATH, or when the CLI is
    # installed in a non-standard location.
    command_path: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    # OpenRouter slug for pricing lookup. Each frontier CLI represents one
    # declared model in the conclave — set this to the slug OpenRouter uses
    # for that exact model (e.g. "anthropic/claude-sonnet-4.6") so the
    # Pricing view shows accurate $/M rates when the seat is in API mode.
    model_slug: Optional[str] = None
    # Reasoning effort for CLIs that expose one as a per-session flag
    # (Antigravity's `--effort low|medium|high`). None = the CLI's default.
    effort: Optional[str] = None


class OpenRouterModel(ConfigModel):
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


class OpenRouterConfig(ConfigModel):
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


class EvidenceConfig(ConfigModel):
    """Provider-neutral evidence acquisition limits and optional search endpoint."""

    enabled: bool = True
    require_https: bool = True
    max_urls_per_request: int = Field(default=5, ge=1, le=20)
    max_document_bytes: int = Field(default=1_000_000, ge=10_000, le=10_000_000)
    max_document_chars: int = Field(default=80_000, ge=1_000, le=500_000)
    timeout_seconds: float = Field(default=12.0, ge=1.0, le=60.0)
    search_endpoint: Optional[str] = None
    search_api_key_env: Optional[str] = None
    search_api_key_header: str = "Authorization"


class Config(ConfigModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)


def is_loopback_host(host: str) -> bool:
    """Return whether a configured bind host is limited to this machine."""
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def api_token(config: Config) -> Optional[str]:
    """Resolve API authentication token; environment takes precedence."""
    return os.environ.get("CONCLAVE_API_TOKEN") or config.server.api_token


def validate_server_boundary(config: Config) -> None:
    """Refuse accidental unauthenticated LAN or Internet exposure."""
    if is_loopback_host(config.server.host):
        return
    if not config.server.allow_remote:
        raise ValueError(
            "server.host is non-loopback; set server.allow_remote=true and configure "
            "server.api_token (or CONCLAVE_API_TOKEN) to acknowledge remote exposure"
        )
    token = api_token(config)
    if not token or len(token) < 16:
        raise ValueError(
            "remote binding requires CONCLAVE_API_TOKEN or server.api_token with at least 16 characters"
        )


def _resolve_config_path() -> Optional[Path]:
    """Walk the DR0016 discovery order and return the first config file that
    exists, or None if we should fall through to built-in `Config()` defaults.

    Performs first-run seeding: if `<user_data_root>/config.yaml` is missing
    AND we're not in dev-mode AND `./config.example.yaml` is packaged with
    the app, copy the example into user_data_root once.
    """
    # Step 2: explicit env var
    env_override = os.environ.get("SWITCHBOARD_CONFIG")
    if env_override:
        p = Path(env_override)
        if p.exists():
            return p
        logger.warning("SWITCHBOARD_CONFIG=%s does not exist; falling through.", env_override)

    # Step 3+4: user_config_path (packaged) vs ./config.yaml (dev).
    # We import lazily so this module can be imported in tests without
    # triggering user_data_root() resolution.
    from app.utils.paths import is_dev_mode, user_config_path

    if not is_dev_mode():
        user_cfg = user_config_path()
        if user_cfg.exists():
            return user_cfg

        # First-run seed: copy packaged example into user_data_root.
        packaged_example = _find_packaged_example()
        if packaged_example is not None:
            try:
                shutil.copy2(packaged_example, user_cfg)
                logger.info("First-run config seed: %s -> %s", packaged_example, user_cfg)
                return user_cfg
            except OSError as e:
                logger.warning("First-run config seed failed (%s); using built-in defaults.", e)

        return None  # fall through to Config() defaults

    # Dev mode: prefer ./config.yaml, then ./config.example.yaml.
    for candidate in (Path("config.yaml"), Path("config.example.yaml")):
        if candidate.exists():
            return candidate
    return None


def _find_packaged_example() -> Optional[Path]:
    """Locate `config.example.yaml` bundled with the application.

    First tries `importlib.resources` against the `app` package (this is
    how a frozen/packaged build exposes its data files). Falls back to a
    repo-relative lookup so dev installs still work.
    """
    try:
        from importlib import resources

        # In dev install: traverses the source tree. In a frozen build with
        # `config.example.yaml` collected as a data file alongside the app
        # package, this also resolves correctly.
        candidate = resources.files("app").joinpath("..", "config.example.yaml")
        as_path = Path(str(candidate)).resolve()
        if as_path.is_file():
            return as_path
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass

    fallback = Path("config.example.yaml")
    if fallback.is_file():
        return fallback.resolve()
    return None


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML; fall back to bundled example or built-in defaults.

    See module docstring for the full discovery order.
    """
    if path is not None:
        cfg_path = Path(path)
        if not cfg_path.exists():
            logger.warning("load_config: %s does not exist; using defaults.", cfg_path)
            return Config()
    else:
        cfg_path = _resolve_config_path()

    if cfg_path is None:
        return Config()

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


_cached_config: Optional[Config] = None


def get_config() -> Config:
    """Return the singleton Config, loading it on first call.

    Lazy: nothing happens until the first invocation. Callers stop importing
    `config` from `app.main` (the module-import-time global is removed).
    """
    global _cached_config
    if _cached_config is None:
        _cached_config = load_config()
    return _cached_config


def reset_cache() -> None:
    """Clear the cached Config. Test-only — production code should never call
    this. Tests that need a fresh load between cases use it.
    """
    global _cached_config
    _cached_config = None
