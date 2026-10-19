"""MCP server (Streamable HTTP) exposing the generator as agent tools.

Mounted at ``/mcp`` next to the REST API, so one deployment serves both. The
transport is stateless + JSON so no session state has to survive Render's
spin-down.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

from .config import settings
from .models import GenerateRequest, resolve_plan
from .perchance import PerchanceClient
from .presets import describe as describe_presets
from .styles import get_catalog

# DNS-rebinding protection is aimed at locally bound servers. A public Render
# service cannot know its hostname at build time, so it is opt-in via
# MCP_ALLOWED_HOSTS; nothing here is cookie- or session-authenticated.
_allowed_hosts = settings.mcp_allowed_hosts

mcp = FastMCP(
    "perchance-image-generator",
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(_allowed_hosts),
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_hosts,
    ),
    instructions=(
        "Free text-to-image generation backed by Perchance. "
        "Call get_options first to discover styles, shapes and presets."
    ),
)


def _client() -> PerchanceClient:
    """Resolve the process-wide client created by the FastAPI lifespan."""
    from .state import get_client

    return get_client()


@mcp.tool()
def get_options() -> dict:
    """List every available parameter: styles, shapes, presets and limits.

    Call this before generate_image to see the valid values.
    """
    catalog = get_catalog()
    return {
        "styles": [s["name"] for s in catalog.describe()],
        "shapes": {
            "square": "768x768",
            "portrait": "512x768",
            "landscape": "768x512",
        },
        "presets": describe_presets(),
        "defaults": {
            "style": settings.default_style,
            "shape": settings.default_shape,
            "guidance_scale": settings.default_guidance_scale,
            "seed": -1,
        },
        "limits": {"max_count": settings.max_count, "max_prompt_chars": settings.max_prompt_chars},
    }


@mcp.tool()
def list_styles() -> list[str]:
    """Return the name of every available style."""
    return [style.name for style in get_catalog().styles]


@mcp.tool()
def service_status() -> dict:
    """Report service health and whether the upstream user key is still valid."""
    client = _client()
    return {"stats": client.stats.snapshot(), "config": {
        "key_pool_size": settings.key_pool_size,
        "max_count": settings.max_count,
    }}


@mcp.tool()
async def generate_image(
    prompt: str,
    style: str | None = None,
    preset: str | None = None,
    negative_prompt: str | None = None,
    shape: str | None = None,
    resolution: str | None = None,
    seed: int | None = None,
    guidance_scale: float | None = None,
    count: int = 1,
):  # noqa: ANN201 - FastMCP treats an unannotated return as unstructured image content
    """Generate image(s) from a text prompt and return them as image content.

    Args:
        prompt: What to draw.
        style: Style name from list_styles (e.g. "Watercolor"). Omit for no style.
        preset: "coloring_page" or "cinematic_watercolor" to apply a known recipe.
        negative_prompt: Things to avoid.
        shape: "square", "portrait" or "landscape".
        resolution: Explicit "WxH", e.g. "768x512".
        seed: Fixed seed for reproducibility; omit for random.
        guidance_scale: Prompt adherence, 1-20 (Perchance default 7).
        count: How many images (1-8 by default).
    """
    request = GenerateRequest(
        prompt=prompt,
        style=style,
        preset=preset,
        negative_prompt=negative_prompt,
        shape=shape,  # type: ignore[arg-type]
        resolution=resolution,
        seed=seed,
        guidance_scale=guidance_scale,
        count=count,
    )
    plan = resolve_plan(request)
    client = _client()
    results = []
    for _ in range(request.count):
        result = await client.generate_image(
            prompt=plan.prompt,
            negative_prompt=plan.negative_prompt,
            seed=plan.seed,
            resolution=plan.resolution,
            guidance_scale=plan.guidance_scale,
        )
        results.append(
            Image(data=result.data, format=result.content_type.split("/")[-1])
        )
    return results


def build_app():
    """Return the Starlette sub-application to mount at /mcp."""
    return mcp.streamable_http_app()
