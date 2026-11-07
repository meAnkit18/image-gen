"""Environment-driven configuration.

Everything is read once at import time from environment variables so the same
image works locally and on Render without code changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _str(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _str(name)
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


#: Resolutions Perchance exposes through its own shape dropdown.
SHAPE_RESOLUTIONS: dict[str, str] = {
    "square": "768x768",
    "portrait": "512x768",
    "landscape": "768x512",
}


@dataclass(frozen=True)
class Settings:
    """Runtime settings."""

    # --- upstream -------------------------------------------------------
    # Remote browser worker (preferred when configured).
    browser_worker_url: str | None = _str("BROWSER_WORKER_URL")
    browser_worker_key: str | None = _str("BROWSER_WORKER_KEY")
    browser_worker_timeout: float = _float("BROWSER_WORKER_TIMEOUT", 360.0)

    user_key: str | None = _str("PERCHANCE_USER_KEY")
    browser_id: str | None = _str("PERCHANCE_BROWSER_ID")
    impersonate: str = _str("PERCHANCE_IMPERSONATE", "firefox") or "firefox"
    upstream_timeout: float = _float("UPSTREAM_TIMEOUT", 120.0)
    generate_deadline: float = _float("GENERATE_DEADLINE", 240.0)
    #: Number of independent upstream identities (= max parallel generations).
    key_pool_size: int = max(1, _int("KEY_POOL_SIZE", 3))

    # --- API surface ----------------------------------------------------
    default_style: str = _str("DEFAULT_STYLE", "No style") or "No style"
    default_shape: str = _str("DEFAULT_SHAPE", "square") or "square"
    default_guidance_scale: float = _float("DEFAULT_GUIDANCE_SCALE", 7.0)
    max_count: int = max(1, _int("MAX_COUNT", 8))
    hard_max_count: int = 32
    max_prompt_chars: int = _int("MAX_PROMPT_CHARS", 2500)

    # --- serving --------------------------------------------------------
    api_key: str | None = _str("API_KEY")
    rate_limit_per_minute: int = _int("RATE_LIMIT_PER_MINUTE", 30)
    cors_origins: list[str] = field(default_factory=lambda: _list("CORS_ORIGINS", ["*"]))
    expose_docs: bool = _bool("EXPOSE_DOCS", True)
    log_level: str = (_str("LOG_LEVEL", "info") or "info").upper()
    enable_mcp: bool = _bool("ENABLE_MCP", True)
    #: Comma-separated Host values for MCP's DNS-rebinding check. Empty (default)
    #: disables it, which is right for a public deployment whose Render hostname
    #: is not known at build time.
    mcp_allowed_hosts: list[str] = field(default_factory=lambda: _list("MCP_ALLOWED_HOSTS"))

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)


settings = Settings()
