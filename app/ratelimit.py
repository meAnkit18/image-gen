"""In-memory sliding-window rate limiter (per client IP).

Good enough for a single free-tier instance: it protects the upstream from a
runaway caller without needing Redis.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``."""
        if self.limit <= 0:
            return True, 0
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - self.window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(1, int(self.window - (now - bucket[0])) + 1)
                return False, retry_after
            bucket.append(now)
            if len(self._hits) > 4096:  # crude bound on memory
                for stale in [k for k, v in self._hits.items() if not v][:1024]:
                    self._hits.pop(stale, None)
            return True, 0
