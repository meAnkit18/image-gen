"""Typed errors that map cleanly onto HTTP status codes."""
from __future__ import annotations


class PerchanceError(Exception):
    """Base class for upstream failures."""

    status_code = 502
    code = "upstream_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


class UpstreamUnavailable(PerchanceError):
    """Network failure or unexpected upstream payload."""

    status_code = 503
    code = "upstream_unavailable"


class KeyUnavailable(PerchanceError):
    """Could not obtain a verified upstream user key."""

    status_code = 503
    code = "key_unavailable"


class GenerationTimeout(PerchanceError):
    """Perchance never returned a finished image in time."""

    status_code = 504
    code = "generation_timeout"


class UpstreamRejected(PerchanceError):
    """Perchance refused the request (moderation, bad params, ...)."""

    status_code = 422
    code = "upstream_rejected"


class RateLimited(PerchanceError):
    status_code = 429
    code = "rate_limited"


class Unauthorized(PerchanceError):
    status_code = 401
    code = "unauthorized"
