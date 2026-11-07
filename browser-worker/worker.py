"""Perchance browser worker.

Runs the real Perchance UI in a single headless browser context, then extracts
new inline data-URL images from the nested image-generation embed frames.
Only one generation is accepted at a time.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright

API_KEY = os.environ.get("WORKER_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))
GENERATION_TIMEOUT = int(os.environ.get("GENERATION_TIMEOUT", "300"))
MAX_PROMPT = int(os.environ.get("MAX_PROMPT_CHARS", "2500"))
SITE = "https://perchance.org/ai-text-to-image-generator"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

browser = None
playwright = None
context = None
page = None
generation_lock = asyncio.Lock()


class GenerateBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT)
    style: str = "Watercolor"
    shape: str = "Square"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser, playwright, context, page
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-extensions",
            "--no-first-run",
            "--no-zygote",
            "--disable-features=site-per-process",
            "--renderer-process-limit=2",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1400, "height": 900},
        user_agent=UA,
    )
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    page = await context.new_page()
    await page.goto(SITE, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(6000)
    if not any(
        ".perchance.org/ai-text-to-image" in frame.url and "image-generation" not in frame.url
        for frame in page.frames
    ):
        raise RuntimeError("Perchance generator frame did not load during worker startup")
    yield
    await browser.close()
    await playwright.stop()


app = FastAPI(title="Perchance Browser Worker", lifespan=lifespan)


def auth(value: str | None) -> None:
    if not API_KEY:
        raise HTTPException(503, "WORKER_API_KEY is not configured")
    if value != API_KEY:
        raise HTTPException(401, "invalid worker key")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "browser": bool(browser and browser.is_connected())}


@app.get("/readyz")
async def readyz():
    generator_ready = bool(
        page and any(
            ".perchance.org/ai-text-to-image" in frame.url
            and "image-generation" not in frame.url
            for frame in page.frames
        )
    )
    healthy = bool(browser and browser.is_connected() and generator_ready)
    return {
        "status": "ready" if healthy else "degraded",
        "browser": bool(browser and browser.is_connected()),
        "generator_frame": generator_ready,
    }


async def gallery_sources() -> set[str]:
    found: set[str] = set()
    for frame in page.frames:
        if "image-generation.perchance.org/embed" not in frame.url:
            continue
        try:
            sources = await frame.evaluate(
                "() => Array.from(document.querySelectorAll('img')).map(i => i.src)"
            )
            found.update(sources)
        except Exception:
            continue
    return found


async def select_controls(frame, style: str, shape: str) -> list[str]:
    return await frame.evaluate(
        """({style,shape}) => {
          const sels = Array.from(document.querySelectorAll('select'));
          const pick = (s, needle) => {
            if (!s) return 'NOSEL';
            const opts = Array.from(s.options);
            const o = opts.find(x => x.text.trim() === needle)
                || opts.find(x => x.text.trim().toLowerCase() === needle.toLowerCase())
                || opts.find(x => x.text.indexOf(needle) >= 0);
            if (!o) return 'MISS:' + needle;
            s.selectedIndex = opts.indexOf(o);
            s.dispatchEvent(new Event('input',{bubbles:true}));
            s.dispatchEvent(new Event('change',{bubbles:true}));
            return 'OK:' + o.text.trim();
          };
          const styleSel = sels.find(s => Array.from(s.options).some(o => o.text.includes('Painted Anime')));
          const shapeSel = sels.find(s => Array.from(s.options).some(o => ['Square','Portrait','Landscape'].includes(o.text.trim())));
          const countSel = sels.find(s => {
            const a = Array.from(s.options).map(o=>o.text.trim());
            return a.includes('2') && a.length <= 8;
          });
          const a = pick(styleSel,style);
          const b = pick(shapeSel,shape);
          const c = pick(countSel,'2');
          if (a.startsWith('MISS') || b.startsWith('MISS') || c.startsWith('MISS') ||
              a==='NOSEL'||b==='NOSEL'||c==='NOSEL') throw new Error([a,b,c].join(','));
          return [a,b,c];
        }""",
        {"style": style, "shape": shape},
    )


async def set_prompt(frame, prompt: str) -> None:
    await frame.evaluate(
        """(p) => {
          const tas = Array.from(document.querySelectorAll('textarea.paragraph-input'));
          const t = tas.find(x => !(x.placeholder||'').toLowerCase().includes('store prompts')) || tas[tas.length-1];
          if (!t) throw new Error('prompt textarea not found');
          t.focus(); t.value = p;
          t.dispatchEvent(new Event('input',{bubbles:true}));
          t.dispatchEvent(new Event('change',{bubbles:true}));
        }""",
        prompt,
    )


async def capture_new_image(baseline: set[str]) -> str | None:
    deadline = time.monotonic() + GENERATION_TIMEOUT
    while time.monotonic() < deadline:
        await page.wait_for_timeout(2000)
        for frame in page.frames:
            if "image-generation.perchance.org/embed" not in frame.url:
                continue
            try:
                imgs = await frame.evaluate(
                    """() => Array.from(document.querySelectorAll('img')).map(i => ({
                      src:i.src, width:i.naturalWidth
                    }))"""
                )
            except Exception:
                continue
            for img in imgs:
                src = img.get("src", "")
                if src.startswith("data:image/") and img.get("width", 0) > 200 and src not in baseline:
                    return src
    return None


@app.post("/generate")
async def generate(body: GenerateBody, x_worker_key: str | None = Header(None, alias="X-Worker-Key")):
    auth(x_worker_key)
    if generation_lock.locked():
        raise HTTPException(409, "a generation is already in progress; retry shortly")

    async with generation_lock:
        try:
            gen = next(
                f for f in page.frames
                if ".perchance.org/ai-text-to-image" in f.url
                and "image-generation" not in f.url
            )
        except StopIteration:
            await page.reload(wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(6000)
            try:
                gen = next(
                    f for f in page.frames
                    if ".perchance.org/ai-text-to-image" in f.url
                    and "image-generation" not in f.url
                )
            except StopIteration:
                raise HTTPException(502, "Perchance generator frame not found")

        try:
            controls = await select_controls(gen, body.style, body.shape)
            await set_prompt(gen, body.prompt)
            baseline = await gallery_sources()
            button = gen.locator('button:has-text("generate")').first
            await button.click(timeout=15000)
            data_url = await capture_new_image(baseline)
            if not data_url:
                raise HTTPException(504, "Perchance did not produce a new image before timeout")
            raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
            if len(raw) < 1024:
                raise HTTPException(502, "Perchance returned an unexpectedly small image")
            return Response(
                content=raw,
                media_type="image/jpeg",
                headers={
                    "X-Image-Bytes": str(len(raw)),
                    "X-Browser-Controls": ",".join(controls),
                    "Cache-Control": "no-store",
                },
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"browser generation failed: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("worker:app", host="0.0.0.0", port=PORT, workers=1)
