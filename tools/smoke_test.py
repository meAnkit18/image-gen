"""Live smoke test for the deployed Perchance API.

Usage:
  python tools/smoke_test.py https://service.onrender.com YOUR_API_KEY

This performs one real upstream generation and saves the returned JPEG.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python tools/smoke_test.py BASE_URL API_KEY", file=sys.stderr)
        return 2

    base_url, api_key = sys.argv[1].rstrip("/"), sys.argv[2]
    headers = {"X-API-Key": api_key}
    timeout = httpx.Timeout(300.0, connect=30.0)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        health = client.get(f"{base_url}/healthz")
        print(f"health: HTTP {health.status_code} {health.text[:300]}")
        if health.status_code != 200:
            return 1

        options = client.get(f"{base_url}/v1/options")
        print(f"options: HTTP {options.status_code}")
        if options.status_code != 200:
            print(options.text[:1000])
            return 1

        response = client.post(
            f"{base_url}/v1/generate",
            headers=headers,
            json={
                "prompt": "a small red lighthouse on a rocky coast at sunset, clean digital illustration",
                "style": "Watercolor",
                "shape": "square",
                "resolution": "512x512",
                "response_format": "image",
                "count": 1,
            },
        )
        print(f"generate: HTTP {response.status_code}")
        print(f"content-type: {response.headers.get('content-type')}")
        print(f"request-id: {response.headers.get('X-Request-Id')}")
        print(f"generation-ms: {response.headers.get('X-Generation-Ms')}")
        if response.status_code != 200:
            print(response.text[:2000])
            return 1

        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            print(f"Expected image/* response, got {content_type}")
            print(response.text[:1000])
            return 1

        out = Path("perchance-smoke-test.jpg")
        out.write_bytes(response.content)
        if len(response.content) < 1024:
            print(f"Response suspiciously small: {len(response.content)} bytes")
            return 1
        print(f"saved: {out.resolve()} ({len(response.content)} bytes)")
        print("PASS: API returned image bytes.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
