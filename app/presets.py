"""Prompt presets carried over from the Perchance skill.

A preset is just a documented suffix plus the style/shape that were validated
with it, so callers can reproduce the two recipes the skill proved out:
toddler coloring pages, and cinematic European watercolour.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    id: str
    description: str
    suffix: str
    style: str
    shape: str
    #: Appended to the rendered prompt before `suffix` is applied.
    negative_prompt: str = ""


PRESETS: dict[str, Preset] = {
    "coloring_page": Preset(
        id="coloring_page",
        description="Toddler coloring-book page: thick black outlines, white fill, no shading.",
        suffix=(
            ", children coloring book page, pure black outlines only, thick bold lines, "
            "white fill, no solid black areas, no gray shading, no colors, one single "
            "centered subject, plain white background"
        ),
        style="Concept Sketch",
        shape="portrait",
    ),
    "cinematic_watercolor": Preset(
        id="cinematic_watercolor",
        description="Cinematic European hand-painted watercolour film still.",
        suffix=(
            ", cinematic European hand-painted watercolor gouache, loose dark ink linework, "
            "visible brush strokes, textured paper, muted earthy palette warm beige dusty "
            "brown faded olive muted blue charcoal cream, soft natural light, atmospheric, "
            "vintage European animated film still, 16:9 widescreen, no photorealism no 3D "
            "no CGI no anime no vector"
        ),
        style="Watercolor",
        shape="landscape",
    ),
}


def get_preset(name: str | None) -> Preset | None:
    if not name:
        return None
    return PRESETS.get(name.strip().lower())


def describe() -> list[dict[str, str]]:
    return [
        {
            "id": p.id,
            "description": p.description,
            "suffix": p.suffix,
            "recommended_style": p.style,
            "recommended_shape": p.shape,
        }
        for p in PRESETS.values()
    ]
