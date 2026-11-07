"""FastAPI application: REST + MCP endpoints for Perchance image generation."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
import uuid
import zipfile
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .config import SHAPE_RESOLUTIONS, settings
from .errors import PerchanceError, RateLimited, Unauthorized
from .models import GenerateRequest, GenerationMeta, GenerationResponse, ImageMeta, resolve_plan
from .perchance import PerchanceClient
from .presets import describe as describe_presets
from .ratelimit import SlidingWindowLimiter
from .state import get_client, set_client
from .styles import get_catalog

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("perchance.api")

limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = PerchanceClient(settings)
    worker = None
    if settings.browser_worker_url and settings.browser_worker_key:
        worker = BrowserWorkerClient(
            settings.browser_worker_url,
            settings.browser_worker_key,
            settings.browser_worker_timeout,
        )
        app.state.browser_worker = worker
        log.info("browser worker backend configured")
    else:
        app.state.browser_worker = None
        log.warning("browser worker not configured; legacy direct HTTP backend is active")
    set_client(client)
    log.info(
        "startup: docs=%s mcp=%s auth=%s rate_limit=%s/min pool=%d",
        settings.expose_docs,
        settings.enable_mcp,
        settings.auth_enabled,
        settings.rate_limit_per_minute,
        settings.key_pool_size,
    )
    if settings.enable_mcp:
        from .mcp import mcp

        mcp.streamable_http_app()  # creates the lazily-built session manager
        async with mcp.session_manager.run():
            yield
    else:
        yield
    if worker is not None:
        await worker.close()
    log.info("shutdown")


app = FastAPI(
    title="Perchance Image Generator API",
    version="1.0.0",
    summary="Free Perchance text-to-image generation using a browser worker or legacy HTTP backend.",
    description=(
        "A thin API gateway for Perchance image generation. When a browser worker is configured, it drives the real Perchance webpage with Playwright and captures the inline generated image.\n\n"
        "* `POST /v1/generate` returns the image bytes directly.\n"
        "* `GET /v1/options` lists every tunable parameter.\n"
        "* `GET /v1/styles` lists all 79 upstream styles.\n"
        "* `POST /mcp` speaks MCP (Streamable HTTP) for agent clients.\n\n"
        "Deployed on Render's free tier, which sleeps after 15 idle minutes - "
        "the first request afterwards pays a ~60 s cold start."
    ),
    docs_url="/docs" if settings.expose_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.expose_docs else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id", "X-Image-Seed", "X-Generation-Ms", "Retry-After"],
)


@app.middleware("http")
async def normalise_mcp_path(request: Request, call_next):
    """Serve /mcp directly instead of 307-redirecting to /mcp/.

    MCP clients POST to the configured URL; a redirect there is a common source
    of confusing client failures, so rewrite the scope before routing.
    """
    if settings.enable_mcp and request.scope.get("path") == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    return await call_next(request)


# --------------------------------------------------------------------- auth
def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def guard(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None),
) -> None:
    """Optional API-key auth, then a per-IP rate limit."""
    if settings.auth_enabled:
        presented = x_api_key
        if not presented and authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        if presented != settings.api_key:
            raise Unauthorized("missing or invalid API key")

    allowed, retry_after = limiter.allow(client_ip(request))
    if not allowed:
        raise RateLimited(
            f"rate limit exceeded ({settings.rate_limit_per_minute}/min); retry in {retry_after}s"
        )


@app.exception_handler(422)
async def validation_error_handler(request: Request, exc):
    """Log request validation failures without logging request bodies or secrets."""
    errors = exc.errors() if hasattr(exc, "errors") else []
    safe_errors = [
        {"loc": error.get("loc"), "msg": error.get("msg"), "type": error.get("type")}
        for error in errors
    ]
    log.warning("request validation failed method=%s path=%s errors=%s", request.method, request.url.path, safe_errors)
    return JSONResponse(status_code=422, content={"detail": errors})


class BrowserWorkerClient:
    """HTTP client for the remote Playwright browser worker."""

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        import httpx
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=20.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict:
        try:
            response = await self._client.get(f"{self.base_url}/readyz")
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}

    async def generate(self, *, prompt: str, style: str, shape: str) -> tuple[bytes, str]:
        from .errors import UpstreamUnavailable, UpstreamRejected
        try:
            response = await self._client.post(
                f"{self.base_url}/generate",
                headers={"X-Worker-Key": self.api_key},
                json={"prompt": prompt, "style": style, "shape": shape.title()},
            )
        except Exception as exc:
            raise UpstreamUnavailable(f"browser worker request failed: {exc}") from exc
        if response.status_code == 409:
            raise UpstreamUnavailable("browser worker is busy; retry shortly")
        if response.status_code in (400, 422):
            raise UpstreamRejected(f"browser worker rejected request: {response.text[:800]}")
        if response.status_code >= 400:
            raise UpstreamUnavailable(
                f"browser worker returned HTTP {response.status_code}: {response.text[:800]}"
            )
        content_type = response.headers.get("content-type", "image/jpeg")
        if not content_type.startswith("image/") or len(response.content) < 1024:
            raise UpstreamUnavailable("browser worker returned invalid or empty image bytes")
        return response.content, content_type


@app.exception_handler(PerchanceError)
async def perchance_error_handler(_: Request, exc: PerchanceError) -> JSONResponse:
    headers = {}
    if exc.status_code == 429:
        headers["Retry-After"] = "60"
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict(), headers=headers)


# ------------------------------------------------------------------ helpers
def _build_response(
    request: GenerateRequest,
    plan,
    results: list,
    request_id: str,
    elapsed_ms: int,
    failed: int,
) -> Response:
    metas = [
        ImageMeta(
            index=index,
            seed=result.seed,
            width=result.width,
            height=result.height,
            content_type=result.content_type,
            bytes=len(result.data),
            maybe_nsfw=result.maybe_nsfw,
            upstream_image_id=result.upstream_image_id,
            elapsed_ms=result.elapsed_ms,
            b64=(
                base64.b64encode(result.data).decode()
                if request.response_format == "json"
                else None
            ),
        )
        for index, result in enumerate(results)
    ]
    generation_meta = GenerationMeta(
        request_id=request_id,
        style=plan.style,
        shape=plan.shape,
        preset=plan.preset,
        resolution=plan.resolution,
        guidance_scale=plan.guidance_scale,
        prompt_sent=plan.prompt,
        negative_prompt_sent=plan.negative_prompt,
        count=request.count,
        failed=failed,
        total_elapsed_ms=elapsed_ms,
    )
    common_headers = {
        "X-Request-Id": request_id,
        "X-Generation-Ms": str(elapsed_ms),
        "Cache-Control": "no-store",
    }
    if results:
        common_headers["X-Image-Seed"] = str(results[0].seed)

    if request.response_format == "json":
        return JSONResponse(
            content=json.loads(
                GenerationResponse(meta=generation_meta, images=metas).model_dump_json()
            ),
            headers=common_headers,
        )

    if request.count == 1 and request.response_format == "image":
        return Response(
            content=results[0].data,
            media_type=results[0].content_type,
            headers=common_headers,
        )

    buffer = io.BytesIO()
    extension = "jpg"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, result in enumerate(results, start=1):
            extension = "png" if "png" in result.content_type else "jpg"
            archive.writestr(f"image-{index}.{extension}", result.data)
        archive.writestr(
            "metadata.json",
            GenerationResponse(meta=generation_meta, images=metas).model_dump_json(indent=2),
        )
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={**common_headers, "Content-Disposition": f'attachment; filename="{request_id}.zip"'},
    )


# ------------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs" if settings.expose_docs else "/v1/options")


@app.get("/healthz", tags=["ops"], summary="Liveness probe (no upstream call)")
async def healthz() -> dict:
    return {"status": "ok", "uptime_s": round(time.time() - get_client().stats.started_at, 1)}


@app.get("/readyz", tags=["ops"], summary="Readiness probe (verifies the upstream key)")
async def readyz() -> JSONResponse:
    status = await get_client().check_key()
    healthy = status.get("status") in {"verified", "unreachable"}
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "degraded", "upstream": status},
    )


@app.get("/v1/options", tags=["meta"], summary="Every available parameter")
async def options() -> dict:
    """Full parameter catalog: styles, shapes, resolutions, presets and limits."""
    catalog = get_catalog()
    return {
        "endpoints": {
            "generate": "POST /v1/generate",
            "options": "GET /v1/options",
            "styles": "GET /v1/styles",
            "mcp": "POST /mcp",
            "health": "GET /healthz",
        },
        "prompt": {
            "type": "string",
            "required": True,
            "max_chars": settings.max_prompt_chars,
            "description": "What to draw.",
        },
        "style": {
            "type": "string",
            "default": settings.default_style,
            "description": "Any name or id from GET /v1/styles. Unknown values fall back to no style.",
            "count": len(catalog.styles),
        },
        "preset": {
            "type": "string",
            "default": None,
            "options": [p["id"] for p in describe_presets()],
            "description": "Named recipe; explicit fields override it.",
        },
        "shape": {
            "type": "string",
            "default": settings.default_shape,
            "options": SHAPE_RESOLUTIONS,
        },
        "resolution": {
            "type": "string",
            "default": "depends on shape",
            "format": "WxH",
            "description": "Explicit override, e.g. 640x640. Sides 256-1536, multiples of 8.",
        },
        "negative_prompt": {"type": "string", "default": "", "description": "Things to avoid."},
        "suffix": {"type": "string", "default": "", "description": "Text appended to the prompt."},
        "seed": {
            "type": "integer",
            "default": -1,
            "description": "-1 or omitted = random. The seed actually used is returned.",
        },
        "guidance_scale": {
            "type": "number",
            "default": settings.default_guidance_scale,
            "min": 1,
            "max": 20,
        },
        "count": {
            "type": "integer",
            "default": 1,
            "min": 1,
            "max": settings.max_count,
            "hard_max": settings.hard_max_count,
        },
        "response_format": {"type": "string", "default": "image", "options": ["image", "json", "zip"]},
        "presets": describe_presets(),
        "examples": [
            {
                "summary": "One watercolour image, raw JPEG back",
                "body": {
                    "prompt": "a red lighthouse on a cliff at dusk",
                    "style": "Watercolor",
                    "shape": "landscape",
                },
            },
            {
                "summary": "Four coloring pages as a zip",
                "body": {
                    "prompt": "a smiling baby cow",
                    "preset": "coloring_page",
                    "count": 4,
                    "response_format": "zip",
                },
            },
            {
                "summary": "Reproducible cinematic still",
                "body": {
                    "prompt": "an airline pilot walking across a misty airport apron",
                    "preset": "cinematic_watercolor",
                    "seed": 123456,
                },
            },
        ],
    }


@app.get("/v1/styles", tags=["meta"], summary="All 79 upstream styles")
async def styles(include_templates: bool = False) -> dict:
    entries = get_catalog().describe()
    if not include_templates:
        entries = [{k: v for k, v in e.items() if k != "prompt_template"} for e in entries]
    return {"count": len(entries), "styles": entries}


@app.get("/v1/stats", tags=["ops"], summary="Generation counters")
async def stats(_: None = Depends(guard)) -> dict:
    return get_client().stats.snapshot()


@app.post(
    "/v1/generate",
    tags=["generate"],
    summary="Generate image(s)",
    response_class=Response,
    responses={
        200: {
            "content": {
                "image/jpeg": {},
                "application/zip": {},
                "application/json": {"schema": GenerationResponse.model_json_schema()},
            },
            "description": (
                "Raw image bytes by default. `application/zip` when `count > 1` or "
                "`response_format=zip`; JSON when `response_format=json`."
            ),
        },
        422: {"description": "Invalid parameters or the upstream refused the prompt."},
        429: {"description": "Rate limited."},
        503: {"description": "Upstream unavailable or no user key could be obtained."},
        504: {"description": "Upstream did not finish in time."},
    },
)
async def generate(request: GenerateRequest, _: None = Depends(guard)) -> Response:
    request_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    plan = resolve_plan(request)
    client = get_client()

    log.info(
        "generate %s style=%r shape=%s res=%s count=%d prompt=%r",
        request_id,
        plan.style,
        plan.shape,
        plan.resolution,
        request.count,
        plan.prompt[:80],
    )

    worker = getattr(app.state, "browser_worker", None)
    if worker is not None:
        if request.count != 1:
            raise PerchanceError("browser worker currently supports count=1; send one image per request")
        image_bytes, content_type = await worker.generate(
            prompt=plan.prompt,
            style=plan.style or "No style",
            shape=plan.shape or "square",
        )
        from .perchance import ImageResult
        results = [ImageResult(
            data=image_bytes,
            content_type=content_type,
            seed=plan.seed,
            width=None,
            height=None,
            guidance_scale=plan.guidance_scale,
            prompt=plan.prompt,
            negative_prompt=plan.negative_prompt,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )]
        failures = []
    else:
        tasks = [
            client.generate_image(
                prompt=plan.prompt,
                negative_prompt=plan.negative_prompt,
                seed=plan.seed,
                resolution=plan.resolution,
                guidance_scale=plan.guidance_scale,
            )
            for _ in range(request.count)
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        results = [o for o in outcomes if not isinstance(o, BaseException)]
        failures = [o for o in outcomes if isinstance(o, BaseException)]

        if not results:
            error = failures[0]
            if isinstance(error, PerchanceError):
                raise error
            raise PerchanceError(f"generation failed: {error}") from error

        for failure in failures:
            log.warning("generate %s partial failure: %s", request_id, failure)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return _build_response(
        request, plan, results, request_id, elapsed_ms, failed=len(failures)
    )


if settings.enable_mcp:  # mounted after routes so it does not shadow /docs
    from .mcp import build_app

    app.mount("/mcp", build_app())
