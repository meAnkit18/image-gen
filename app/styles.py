"""Perchance style catalog.

``data/styles.json`` maps each of Perchance's 79 styles to the prompt template the
site itself expands. It was produced in one shot by ``tools/capture_styles.py``:
the tool swaps the description for a sentinel token, clicks generate, and reads
the outgoing API call - so the templates are the real thing rather than a
re-implementation of Perchance's template syntax (which supports ``{a|b}``
choices, filters and cross-plugin variables).
"""
from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

DATA_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / "styles.json"

#: Placeholder the capture tool used in place of the user's description.
SENTINEL = "zqxjkvbwmppromptplaceholder"

_NO_STYLE = {"", "none", "nostyle", "no style", "default", "raw"}


def normalize(name: str) -> str:
    """Fold case, accents and Perchance's math-bold style names to a plain slug.

    ``𝙉𝙤 𝙨𝙩𝙮𝙡𝙚`` and ``No style`` both become ``no style``.
    """
    folded = unicodedata.normalize("NFKD", name or "")
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def slugify(name: str) -> str:
    return normalize(name).replace(" ", "-")


@dataclass(frozen=True)
class Style:
    name: str
    id: str
    template: str
    negative_prompt: str
    is_default: bool = False

    def render(self, prompt: str) -> str:
        """Substitute the user's description into the real Perchance template."""
        if SENTINEL in self.template:
            return self.template.replace(SENTINEL, prompt)
        return f"{prompt}, {self.template}"


class StyleCatalog:
    """Lookup + rendering for the style catalog."""

    def __init__(self, styles: list[Style]) -> None:
        self._styles = styles
        self._by_key: dict[str, Style] = {}
        for style in styles:
            for key in (style.id, normalize(style.name)):
                self._by_key.setdefault(key, style)

    @classmethod
    def load(cls, path: pathlib.Path = DATA_FILE) -> "StyleCatalog":
        raw = json.loads(path.read_text())
        styles: list[Style] = []
        for name, entry in raw.items():
            styles.append(
                Style(
                    name=name,
                    id=slugify(name),
                    template=entry.get("template", SENTINEL),
                    negative_prompt=entry.get("negativePrompt", "") or "",
                    is_default=normalize(name) in _NO_STYLE,
                )
            )
        # Keep "No style" first: it is the honest default.
        styles.sort(key=lambda s: (not s.is_default, s.name.lower()))
        return cls(styles)

    @property
    def styles(self) -> list[Style]:
        return self._styles

    def get(self, name: str | None) -> Style | None:
        if name is None:
            return None
        return self._by_key.get(normalize(name)) or self._by_key.get(name.lower())

    def resolve(self, style_name: str | None, prompt: str) -> tuple[str, str]:
        """Return ``(prompt, negative_prompt)`` for a style + description.

        An unknown style falls back to the raw prompt rather than failing, so a
        style rename upstream never breaks a caller's request.
        """
        if style_name is None or normalize(style_name) in _NO_STYLE:
            return prompt, ""
        style = self.get(style_name)
        if style is None:
            return prompt, ""
        return style.render(prompt), style.negative_prompt

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "name": s.name,
                "id": s.id,
                "no_style": s.is_default,
                "negative_prompt": s.negative_prompt,
                "prompt_template": s.template,
                "template_uses_placeholder": SENTINEL in s.template,
            }
            for s in self._styles
        ]


@lru_cache(maxsize=1)
def get_catalog() -> StyleCatalog:
    return StyleCatalog.load()
