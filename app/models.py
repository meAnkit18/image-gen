"""Request/response schemas and the request -> upstream-plan resolution."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import SHAPE_RESOLUTIONS, settings
from .presets import get_preset
from .styles import get_catalog

RESOLUTION_RE = re.compile(r"^(\d{2,4})x(\d{2,4})$")

Shape = Literal["square", "portrait", "landscape"]
ResponseFormat = Literal["image", "json", "zip"]


class GenerateRequest(BaseModel):
    """Body of ``POST /v1/generate``."""

    prompt: str = Field(
        ...,
        min_length=1,
        description="What to draw. Free text; the style template wraps it.",
        examples=["a red lighthouse on a cliff at dusk, watercolour painting"],
    )
    style: str | None = Field(
        None,
        description=(
            'Style name or id from GET /v1/styles, e.g. "Watercolor", "Concept Sketch", '
            '"Cinematic". Defaults to the server default (no style).'
        ),
    )
    preset: str | None = Field(
        None,
        description=(
            'Named recipe that fills in suffix/style/shape: "coloring_page" or '
            '"cinematic_watercolor". Explicit fields win over the preset.'
        ),
    )
    suffix: str | None = Field(
        None,
        max_length=2000,
        description="Extra text appended to the prompt (before the preset suffix).",
    )
    negative_prompt: str | None = Field(
        None,
        max_length=1000,
        description=(
            "Things to avoid. Merged with any negative prompt the chosen style defines."
        ),
    )
    shape: Shape | None = Field(
        None, description="square (768x768), portrait (512x768) or landscape (768x512)."
    )
    resolution: str | None = Field(
        None,
        description='Explicit "WxH" override, e.g. "768x512". Ignored if `shape` is set.',
        examples=["768x512"],
    )
    seed: int | None = Field(
        None,
        ge=-1,
        le=2_147_483_647,
        description="Omit or -1 for a random seed. The seed used is returned in the response.",
    )
    guidance_scale: float | None = Field(
        None, ge=1, le=20, description="Prompt adherence. Perchance default is 7."
    )
    count: int = Field(
        1, ge=1, le=32, description="How many images to generate (each is a separate generation)."
    )
    response_format: ResponseFormat = Field(
        "image",
        description=(
            "image: raw bytes for a single image. zip: archive when count > 1. "
            "json: metadata plus base64 payloads."
        ),
    )

    @field_validator("resolution")
    @classmethod
    def _check_resolution(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower().replace(" ", "")
        match = RESOLUTION_RE.match(value)
        if not match:
            raise ValueError('resolution must look like "768x512"')
        width, height = int(match.group(1)), int(match.group(2))
        for bound in (width, height):
            if not 256 <= bound <= 1536:
                raise ValueError("resolution sides must be between 256 and 1536")
            if bound % 8:
                raise ValueError("resolution sides must be multiples of 8")
        return f"{width}x{height}"

    @model_validator(mode="after")
    def _check_bounds(self) -> "GenerateRequest":
        if len(self.prompt) > settings.max_prompt_chars:
            raise ValueError(
                f"prompt is {len(self.prompt)} chars; the limit is "
                f"{settings.max_prompt_chars}"
            )
        if self.count > settings.max_count:
            raise ValueError(
                f"count {self.count} exceeds the configured maximum of {settings.max_count}"
            )
        if self.preset and get_preset(self.preset) is None:
            raise ValueError(f"unknown preset {self.preset!r}")
        return self


@dataclass
class Plan:
    """The concrete parameters sent upstream for one image."""

    prompt: str
    negative_prompt: str
    resolution: str
    seed: int
    guidance_scale: float
    style: str | None
    shape: str | None
    preset: str | None


def resolve_plan(request: GenerateRequest) -> Plan:
    """Fold style, preset and suffix into a single upstream prompt."""
    preset = get_preset(request.preset)

    style_name = request.style
    if style_name is None and preset is not None:
        style_name = preset.style
    if style_name is None:
        style_name = settings.default_style

    shape = request.shape
    if shape is None and request.resolution is None and preset is not None:
        shape = preset.shape  # type: ignore[assignment]
    if shape is None and request.resolution is None:
        shape = settings.default_shape  # type: ignore[assignment]

    if request.resolution:
        resolution = request.resolution
    else:
        resolution = SHAPE_RESOLUTIONS.get(shape or "square", SHAPE_RESOLUTIONS["square"])

    prompt = request.prompt.strip()
    if request.suffix:
        prompt = f"{prompt}{request.suffix}"
    if preset is not None:
        prompt = f"{prompt}{preset.suffix}"

    rendered, style_negative = get_catalog().resolve(style_name, prompt)

    negatives = [part for part in (style_negative, request.negative_prompt) if part]
    negative_prompt = ", ".join(part.strip().strip(",") for part in negatives)

    return Plan(
        prompt=rendered,
        negative_prompt=negative_prompt,
        resolution=resolution,
        seed=-1 if request.seed is None else request.seed,
        guidance_scale=(
            settings.default_guidance_scale
            if request.guidance_scale is None
            else request.guidance_scale
        ),
        style=style_name,
        shape=shape,
        preset=request.preset if preset else None,
    )


class ImageMeta(BaseModel):
    index: int
    seed: int | None = None
    width: int | None = None
    height: int | None = None
    content_type: str
    bytes: int
    maybe_nsfw: bool = False
    upstream_image_id: str | None = None
    elapsed_ms: int = 0
    b64: str | None = Field(None, description="Present only when response_format=json.")


class GenerationMeta(BaseModel):
    request_id: str
    style: str | None = None
    shape: str | None = None
    preset: str | None = None
    resolution: str
    guidance_scale: float
    prompt_sent: str
    negative_prompt_sent: str
    count: int
    failed: int = 0
    total_elapsed_ms: int


class GenerationResponse(BaseModel):
    meta: GenerationMeta
    images: list[ImageMeta]
