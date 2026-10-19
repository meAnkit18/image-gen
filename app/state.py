"""Process-wide singletons, kept in their own module to avoid import cycles."""
from __future__ import annotations

from .perchance import PerchanceClient

_client: PerchanceClient | None = None


def set_client(client: PerchanceClient) -> None:
    global _client
    _client = client


def get_client() -> PerchanceClient:
    if _client is None:  # pragma: no cover - only before lifespan startup
        raise RuntimeError("PerchanceClient has not been initialised")
    return _client
