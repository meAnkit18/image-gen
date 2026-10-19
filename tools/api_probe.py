"""Log every image-generation.perchance.org API call the real page makes.

Captures request URL, method, headers-of-interest, body and response status so we
can decide whether the API can be replayed without a browser.

Usage: python3 tools/api_probe.py
"""
import asyncio, json, time

from playwright.async_api import async_playwright

BASE_URL = "https://perchance.org/ai-text-to-image-generator"
PROMPT = "a red lighthouse on a cliff at dusk, watercolour painting"
LOG = []


async def main():
    async with async_playwright() as p:
        b = await p.firefox.launch(headless=True)
        ctx = await b.new_context(
            viewport={"width": 900, "height": 650},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        def on_req(req):
            if "image-generation.perchance.org" in req.url and "/api/" in req.url:
                entry = {"t": round(time.time(), 2), "method": req.method, "url": req.url,
                         "post_data": (req.post_data or "")[:500],
                         "headers": {k: v for k, v in req.headers.items()
                                     if k.lower() in ("content-type", "referer", "origin", "user-agent",
                                                      "accept", "x-requested-with", "cookie")}}
                LOG.append(entry)

        async def on_resp(resp):
            if "image-generation.perchance.org" in resp.url and "/api/" in resp.url:
                try:
                    body = await resp.text()
                except Exception:
                    body = "<unreadable>"
                for e in LOG:
                    if e["url"] == resp.url and "status" not in e:
                        e["status"] = resp.status
                        e["resp_headers"] = dict(resp.headers)
                        e["resp_body"] = body[:600]

        pg = await ctx.new_page()
        pg.on("request", on_req)
        pg.on("response", on_resp)
        await pg.goto(BASE_URL, timeout=90000)
        await pg.wait_for_timeout(7000)
        gen = [f for f in pg.frames
               if ".perchance.org/ai-text-to-image" in f.url and "image-generation" not in f.url][0]
        await gen.evaluate("""(p) => {
          const tas = Array.from(document.querySelectorAll('textarea.paragraph-input'));
          const t = tas.find(x => (x.placeholder||'').indexOf('store prompts') < 0);
          t.focus(); t.value = p;
          t.dispatchEvent(new Event('input',{bubbles:true}));
          t.dispatchEvent(new Event('change',{bubbles:true}));
          const sels = Array.from(document.querySelectorAll('select'));
          const cs = sels.find(x => { const t2 = Array.from(x.options).map(o=>o.text.trim()); return t2.includes('2') && t2.length <= 8; });
          const o = Array.from(cs.options).find(x => x.text.trim() === '2');
          cs.selectedIndex = Array.from(cs.options).indexOf(o);
          cs.dispatchEvent(new Event('input',{bubbles:true}));
          cs.dispatchEvent(new Event('change',{bubbles:true}));
        }""", PROMPT)
        await (await gen.query_selector('button:has-text("generate")')).click()
        await pg.wait_for_timeout(25000)
        out = "/tmp/opencode/api_log_full.json"
        with open(out, "w") as fh:
            json.dump(LOG, fh, indent=2)
        print(f"wrote {out} ({len(LOG)} calls)")
        await b.close()


asyncio.run(main())
