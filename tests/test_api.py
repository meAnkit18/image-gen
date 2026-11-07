"""Unit tests. No network access: the upstream client is replaced with a stub.

Run:  .venv/bin/python -m pytest tests -q
"""
from __future__ import annotations

import base64
import io
import json
import time
import types
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import state
from app.main import app
from app.models import GenerateRequest, resolve_plan
from app.perchance import ImageResult
from app.styles import get_catalog

# A 1x1 JPEG - enough to prove the bytes travel through untouched.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


class FakeClient:
    """Stands in for PerchanceClient."""

    def __init__(self, payload: bytes = TINY_JPEG) -> None:
        self.payload = payload
        self.calls: list[dict] = []
        self.stats = types.SimpleNamespace(
            started_at=time.time(),
            snapshot=lambda: {"generations": len(self.calls)},
        )

    async def generate_image(self, **kwargs) -> ImageResult:
        self.calls.append(kwargs)
        return ImageResult(
            data=self.payload,
            content_type="image/jpeg",
            seed=kwargs.get("seed", -1),
            width=768,
            height=512,
            guidance_scale=kwargs.get("guidance_scale"),
            prompt=kwargs["prompt"],
            negative_prompt=kwargs.get("negative_prompt", ""),
            upstream_image_id="deadbeef",
            elapsed_ms=1234,
        )

    async def check_key(self, key=None):
        return {"status": "verified"}


@pytest.fixture(scope="session")
def client():
    # Session-scoped: the MCP session manager can only be entered once per process.
    with TestClient(app) as test_client:
        fake = FakeClient()
        state.set_client(fake)  # type: ignore[arg-type]
        test_client.fake = fake  # type: ignore[attr-defined]
        yield test_client


# ----------------------------------------------------------------- metadata
def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_options_documents_every_parameter(client):
    body = client.get("/v1/options").json()
    for key in (
        "prompt",
        "style",
        "preset",
        "shape",
        "resolution",
        "negative_prompt",
        "suffix",
        "seed",
        "guidance_scale",
        "count",
        "response_format",
        "presets",
        "examples",
    ):
        assert key in body, f"/v1/options is missing {key}"
    assert body["shape"]["options"] == {
        "square": "768x768",
        "portrait": "512x768",
        "landscape": "768x512",
    }


def test_styles_endpoint_lists_all_79(client):
    body = client.get("/v1/styles").json()
    assert body["count"] == 79
    names = {s["name"] for s in body["styles"]}
    assert {"Watercolor", "Cinematic", "Concept Sketch", "Painterly"} <= names
    # "No style" is the honest default and must sort first.
    assert body["styles"][0]["no_style"] is True


def test_styles_can_include_templates(client):
    body = client.get("/v1/styles", params={"include_templates": True}).json()
    watercolor = next(s for s in body["styles"] if s["name"] == "Watercolor")
    assert "watercolor" in watercolor["prompt_template"].lower()


# ------------------------------------------------------------------- styling
def test_no_style_passes_the_prompt_through():
    assert get_catalog().resolve("No style", "a cat") == ("a cat", "")
    assert get_catalog().resolve(None, "a cat") == ("a cat", "")


def test_unicode_style_name_is_matched():
    """Perchance renders 'No style' with mathematical-bold code points."""
    assert get_catalog().resolve("𝙉𝙤 𝙨𝙩𝙮𝙡𝙚", "a cat") == ("a cat", "")


def test_style_injects_the_real_template():
    prompt, _ = get_catalog().resolve("Watercolor", "a cat")
    assert prompt.startswith("a cat")
    assert "watercolor" in prompt.lower()
    assert "zqxjkvbwmppromptplaceholder" not in prompt


def test_unknown_style_falls_back_to_the_raw_prompt():
    assert get_catalog().resolve("Not A Real Style", "a cat") == ("a cat", "")


def test_style_supplied_negative_prompt_is_merged():
    plan = resolve_plan(GenerateRequest(prompt="x", style="MTG Card", negative_prompt="blurry"))
    assert "low-quality" in plan.negative_prompt
    assert "blurry" in plan.negative_prompt


# ---------------------------------------------------------------- generation
def test_generate_returns_raw_jpeg(client):
    response = client.post("/v1/generate", json={"prompt": "a lighthouse", "style": "Watercolor"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == TINY_JPEG
    assert response.headers["X-Image-Seed"]


def test_generate_defaults_to_square_and_no_style(client):
    client.post("/v1/generate", json={"prompt": "a lighthouse"})
    call = client.fake.calls[-1]  # type: ignore[attr-defined]
    assert call["resolution"] == "768x768"
    assert call["prompt"] == "a lighthouse"


def test_generate_zip_for_multiple_images(client):
    response = client.post(
        "/v1/generate",
        json={"prompt": "a cow", "count": 3, "response_format": "image"},
    )
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "image-1.jpg" in names and "image-3.jpg" in names
        assert "metadata.json" in names
        meta = json.loads(archive.read("metadata.json"))
        assert meta["meta"]["count"] == 3
        assert meta["meta"]["failed"] == 0


def test_generate_json_includes_base64(client):
    response = client.post(
        "/v1/generate",
        json={"prompt": "a cow", "response_format": "json", "seed": 7},
    )
    body = response.json()
    assert body["images"][0]["seed"] == 7
    assert base64.b64decode(body["images"][0]["b64"]) == TINY_JPEG
    assert body["meta"]["resolution"] == "768x768"


def test_preset_fills_style_shape_and_suffix(client):
    client.post("/v1/generate", json={"prompt": "a cow", "preset": "coloring_page"})
    call = client.fake.calls[-1]  # type: ignore[attr-defined]
    assert call["resolution"] == "512x768"  # portrait
    assert "coloring book page" in call["prompt"]
    assert "concept sketch" in call["prompt"].lower()


def test_explicit_fields_beat_the_preset(client):
    client.post(
        "/v1/generate",
        json={"prompt": "a cow", "preset": "coloring_page", "style": "Watercolor", "shape": "square"},
    )
    call = client.fake.calls[-1]  # type: ignore[attr-defined]
    assert call["resolution"] == "768x768"
    assert "watercolor" in call["prompt"].lower()


def test_resolution_override_wins_over_shape(client):
    client.post("/v1/generate", json={"prompt": "x", "shape": "square", "resolution": "512x512"})
    assert client.fake.calls[-1]["resolution"] == "512x512"  # type: ignore[attr-defined]


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "payload,expect",
    [
        ({"prompt": ""}, "at least 1 character"),
        ({"prompt": "x", "shape": "enormous"}, "square"),
        ({"prompt": "x", "resolution": "10x10"}, "between 256 and 1536"),
        ({"prompt": "x", "resolution": "nonsense"}, "must look like"),
        ({"prompt": "x", "preset": "nope"}, "unknown preset"),
        ({"prompt": "x", "response_format": "webp"}, "image"),
    ],
)
def test_bad_input_is_rejected(client, payload, expect):
    response = client.post("/v1/generate", json=payload)
    assert response.status_code == 422
    assert expect in response.text


def test_count_is_capped_by_settings(client):
    response = client.post("/v1/generate", json={"prompt": "x", "count": 999})
    assert response.status_code == 422


def test_unknown_route_404(client):
    assert client.get("/v1/nope").status_code == 404


# ---------------------------------------------------------------------- auth
def test_requests_are_open_when_no_api_key_is_configured(client):
    assert client.get("/v1/stats").status_code == 200


def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.settings",
        types.SimpleNamespace(api_key="s3cret", auth_enabled=True, enable_mcp=True),
    )
    assert client.get("/v1/stats").status_code == 401
    assert client.get("/v1/stats", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/v1/stats", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert (
        client.get("/v1/stats", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    )


def test_mcp_endpoint_has_no_redirect(client):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "perchance-image-generator"

def test_generate_uses_configured_browser_worker(client):
    class FakeWorker:
        async def generate(self, **kwargs):
            assert kwargs["prompt"].startswith("a lighthouse")
            assert kwargs["style"] == "Watercolor"
            return TINY_JPEG + b" " * 2048, "image/jpeg"

    client.app.state.browser_worker = FakeWorker()
    response = client.post("/v1/generate", json={"prompt": "a lighthouse", "style": "Watercolor"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert len(response.content) > 1024
    client.app.state.browser_worker = None

