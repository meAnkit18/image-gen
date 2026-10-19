"""Client for Perchance's image-generation API.

Reverse-engineered from the live site (Oct 2026). The full pipeline is plain
HTTP - no browser, no Cloudflare workaround and no Turnstile token are required,
which is what makes this servable on a 512 MB / 0.1 CPU host:

    GET  /api/verifyUser?browserId=<32 hex>&thread=0
         -> {"status":"success","userKey":"<64 hex>"}
    GET  /api/checkUserVerificationStatus?userKey=<key>&cacheKey=<n>
         -> {"status":"verified"}
    POST /api/generate?userKey=<key>&requestId=<n>&adAccessCode=
         body {"prompt","negativePrompt","seed","resolution","guidanceScale",
               "channel","subChannel","userKey","adAccessCode","requestId"}
         -> {"status":"success","imageDownloadUrl":"/api/downloadTemporaryImageViaProxy?t=...",
             "seed":..,"width":..,"height":..,"guidanceScale":..,"maybeNsfw":..}
    GET  <imageDownloadUrl>  -> image/jpeg bytes

Two upstream quirks drive the design:

* Perchance allows only **one in-flight generation per userKey**. A second
  concurrent request with the same key answers
  ``{"status":"waiting_for_prev_request_to_finish"}``.
* Keys are cheap to mint and long-lived (verified still valid 65+ minutes later),
  so :class:`PerchanceClient` keeps a small pool of independent identities -
  one key per parallel slot - instead of retrying a single key and serialising.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests as curl_requests

from .config import Settings, settings as default_settings
from .errors import (
    GenerationTimeout,
    KeyUnavailable,
    UpstreamRejected,
    UpstreamUnavailable,
)

log = logging.getLogger("perchance")

BASE = "https://image-generation.perchance.org"
EMBED_REFERER = f"{BASE}/embed"
CHANNEL = "ai-text-to-image-generator"
SUBCHANNEL = "public"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

_GET_HEADERS = {
    "user-agent": USER_AGENT,
    "referer": EMBED_REFERER,
    "accept": "*/*",
}
_POST_HEADERS = dict(_GET_HEADERS, **{"content-type": "text/plain;charset=UTF-8"})

#: Upstream statuses that mean "ask again later".
_TRANSIENT = {
    "waiting_for_prev_request_to_finish",
    "havingTechnicalDifficulties",
    "fetch_failure",
    "queue_full",
    "not_in_queue",
}

#: Upstream statuses that will never succeed if retried unchanged.
_PERMANENT = {
    "invalid_prompt",
    "prompt_too_long",
    "content_flagged",
    "banned",
}

_thread_local = threading.local()


def _session(impersonate: str) -> curl_requests.Session:
    """One curl_cffi session per worker thread (sessions are not thread-safe)."""
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = curl_requests.Session(impersonate=impersonate)
        _thread_local.session = sess
    return sess


@dataclass
class ImageResult:
    """A finished generation, already downloaded."""

    data: bytes
    content_type: str
    seed: int | None
    width: int | None
    height: int | None
    guidance_scale: float | None
    prompt: str
    negative_prompt: str
    maybe_nsfw: bool = False
    upstream_image_id: str | None = None
    elapsed_ms: int = 0


@dataclass
class _KeySlot:
    """One upstream identity. Borrowed from the pool, never used concurrently."""

    browser_id: str = field(default_factory=lambda: secrets.token_hex(16))
    user_key: str | None = None
    verified_at: float = 0.0


@dataclass
class Stats:
    generations: int = 0
    failures: int = 0
    keys_minted: int = 0
    total_latency_ms: int = 0
    last_error: str | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def avg_latency_ms(self) -> int:
        return round(self.total_latency_ms / self.generations) if self.generations else 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "generations": self.generations,
            "failures": self.failures,
            "keys_minted": self.keys_minted,
            "avg_latency_ms": self.avg_latency_ms,
            "last_error": self.last_error,
            "uptime_s": round(time.time() - self.started_at, 1),
        }


class PerchanceClient:
    """Async facade over the synchronous curl_cffi calls."""

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or default_settings
        self.stats = Stats()
        self._slots: asyncio.Queue[_KeySlot] = asyncio.Queue()
        self._all_slots: list[_KeySlot] = []
        self._pool_ready = False
        self._pool_lock = asyncio.Lock()
        self._mint_lock = asyncio.Lock()

    # ------------------------------------------------------------------ pool
    async def _ensure_pool(self) -> None:
        if self._pool_ready:
            return
        async with self._pool_lock:
            if self._pool_ready:
                return
            first = _KeySlot()
            if self.cfg.user_key:
                first.user_key = self.cfg.user_key
                first.verified_at = time.time()
                if self.cfg.browser_id:
                    first.browser_id = self.cfg.browser_id
            slots = [first]
            slots.extend(_KeySlot() for _ in range(max(1, self.cfg.key_pool_size) - 1))
            for slot in slots:
                self._slots.put_nowait(slot)
            self._all_slots = slots
            self._pool_ready = True
            log.info("key pool ready (size=%d)", len(slots))

    async def _mint_sync(self, browser_id: str) -> tuple[str, str]:
        return await asyncio.to_thread(self._mint, browser_id)

    def _mint(self, browser_id: str) -> tuple[str, str]:
        url = (
            f"{BASE}/api/verifyUser?browserId={browser_id}"
            f"&thread=0&__cacheBust={time.time()}"
        )
        try:
            resp = _session(self.cfg.impersonate).get(
                url, headers=_GET_HEADERS, timeout=self.cfg.upstream_timeout
            )
            payload = resp.json()
        except Exception as exc:  # network / decode
            raise UpstreamUnavailable(f"verifyUser failed: {exc}") from exc

        status = payload.get("status")
        key = payload.get("userKey")
        if status in {"success", "already_verified"} and key:
            self.stats.keys_minted += 1
            return key, browser_id
        raise KeyUnavailable(
            f"could not obtain a user key (upstream status={status!r}). "
            "Perchance may be challenging this host; set PERCHANCE_USER_KEY to a "
            "key minted from a browser session."
        )

    async def _key_for(self, slot: _KeySlot, *, force: bool = False) -> str:
        if slot.user_key and not force:
            return slot.user_key
        async with self._mint_lock:
            if slot.user_key and not force:
                return slot.user_key
            if force:
                slot.browser_id = secrets.token_hex(16)
                slot.user_key = None
            key, browser_id = await self._mint_sync(slot.browser_id)
            slot.user_key, slot.browser_id, slot.verified_at = key, browser_id, time.time()
            return key

    # -------------------------------------------------------------- upstream
    def _post_generate(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        request_id = f"{time.time()}"
        url = (
            f"{BASE}/api/generate?userKey={key}&requestId={request_id}"
            f"&adAccessCode=&__cacheBust={time.time()}&bdf={time.time()}"
        )
        resp = _session(self.cfg.impersonate).post(
            url,
            data=json.dumps(body),
            headers=_POST_HEADERS,
            timeout=self.cfg.upstream_timeout,
        )
        if resp.status_code >= 500:
            raise UpstreamUnavailable(f"generate http {resp.status_code}")
        try:
            return resp.json()
        except Exception as exc:
            raise UpstreamUnavailable(
                f"generate returned non-JSON (http {resp.status_code})"
            ) from exc

    def _download(self, path: str) -> tuple[bytes, str]:
        url = path if path.startswith("http") else BASE + path
        resp = _session(self.cfg.impersonate).get(
            url, headers=_GET_HEADERS, timeout=self.cfg.upstream_timeout
        )
        if resp.status_code != 200:
            raise UpstreamUnavailable(f"image download http {resp.status_code}")
        content_type = resp.headers.get("content-type") or "image/jpeg"
        return resp.content, content_type

    # ------------------------------------------------------------------ api
    async def check_key(self, key: str | None = None) -> dict[str, Any]:
        """Cheap liveness probe for a user key."""
        await self._ensure_pool()

        async def _probe(candidate: str) -> dict[str, Any]:
            def run() -> dict[str, Any]:
                url = (
                    f"{BASE}/api/checkUserVerificationStatus?userKey={candidate}"
                    f"&cacheKey={int(time.time() / 3)}"
                )
                resp = _session(self.cfg.impersonate).get(
                    url, headers=_GET_HEADERS, timeout=self.cfg.upstream_timeout
                )
                return resp.json()

            return await asyncio.to_thread(run)

        if key:
            try:
                return await _probe(key)
            except Exception as exc:
                return {"status": "unreachable", "detail": str(exc)}
        slot = self._all_slots[0] if self._all_slots else _KeySlot()
        try:
            resolved = await self._key_for(slot)
            return await _probe(resolved)
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}

    async def generate_image(
        self,
        *,
        prompt: str,
        negative_prompt: str = "",
        seed: int = -1,
        resolution: str,
        guidance_scale: float | None = None,
        deadline: float | None = None,
    ) -> ImageResult:
        """Run one generation end to end and return the downloaded bytes."""
        await self._ensure_pool()
        started = time.monotonic()
        budget = deadline if deadline is not None else self.cfg.generate_deadline

        slot = await self._slots.get()
        try:
            key = await self._key_for(slot)
            body = {
                "prompt": prompt,
                "negativePrompt": negative_prompt,
                "seed": seed,
                "resolution": resolution,
                "guidanceScale": (
                    self.cfg.default_guidance_scale
                    if guidance_scale is None
                    else guidance_scale
                ),
                "channel": CHANNEL,
                "subChannel": SUBCHANNEL,
                "userKey": key,
                "adAccessCode": "",
                "requestId": f"{time.time()}",
            }

            payload: dict[str, Any] | None = None
            attempt = 0
            last_status: str | None = None
            while True:
                attempt += 1
                try:
                    payload = await asyncio.to_thread(self._post_generate, key, body)
                    last_status = payload.get("status")
                except UpstreamUnavailable as exc:
                    payload, last_status = None, f"transport_error: {exc}"

                if last_status == "success" and (
                    payload.get("imageDownloadUrl") or payload.get("serverImageDownloadUrl")
                ):
                    break

                if last_status == "invalid_key":
                    log.warning("user key rejected, minting a replacement")
                    key = await self._key_for(slot, force=True)
                    body["userKey"] = key
                elif last_status in _PERMANENT:
                    raise UpstreamRejected(
                        f"Perchance refused the prompt (status={last_status})"
                    )

                if time.monotonic() - started > budget:
                    raise GenerationTimeout(
                        f"no image after {budget:.0f}s (last upstream status={last_status!r})"
                    )
                if last_status not in _TRANSIENT and attempt >= 3:
                    raise UpstreamRejected(
                        f"Perchance returned {last_status!r} {attempt} times"
                    )
                await asyncio.sleep(min(1.0 + 0.75 * attempt, 6.0))

            assert payload is not None
            path = payload.get("imageDownloadUrl") or payload["serverImageDownloadUrl"]
            data, content_type = await asyncio.to_thread(self._download, path)

            elapsed = int((time.monotonic() - started) * 1000)
            self.stats.generations += 1
            self.stats.total_latency_ms += elapsed
            return ImageResult(
                data=data,
                content_type=content_type,
                seed=payload.get("seed"),
                width=payload.get("width"),
                height=payload.get("height"),
                guidance_scale=payload.get("guidanceScale"),
                prompt=payload.get("prompt", prompt),
                negative_prompt=payload.get("negativePrompt", negative_prompt),
                maybe_nsfw=bool(payload.get("maybeNsfw")),
                upstream_image_id=payload.get("imageId"),
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            self.stats.failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._slots.put_nowait(slot)
