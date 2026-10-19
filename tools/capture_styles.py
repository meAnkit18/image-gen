"""One-time capture: resolve every Perchance style's prompt template.

The style dropdown works by having the parent page expand a Perchance template
around your description. We swap the description for a sentinel token, click
generate, and intercept the outgoing /api/generate POST body - which contains the
fully expanded prompt. That gives us an exact style -> template map with no
template-engine reimplementation.

Usage: python3 tools/capture_styles.py [out.json]
"""
import asyncio, json, os, sys

from playwright.async_api import async_playwright

BASE_URL = "https://perchance.org/ai-text-to-image-generator"
SENTINEL = "zqxjkvbwmppromptplaceholder"

SET_STYLE = """(name) => {
  const norm = s => (s||'').normalize('NFKD').replace(/[^A-Za-z0-9]+/g,' ').trim().toLowerCase();
  const sels = Array.from(document.querySelectorAll('select'));
  const sel = sels.find(s => Array.from(s.options).some(o => o.text.indexOf('Painted Anime') >= 0));
  if (!sel) return 'NOSEL';
  const opts = Array.from(sel.options);
  const o = opts.find(x => norm(x.text) === norm(name));
  if (!o) return 'MISS';
  sel.selectedIndex = opts.indexOf(o);
  sel.dispatchEvent(new Event('input', {bubbles: true}));
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return 'OK';
}"""

SET_DESC = """(p) => {
  const t = Array.from(document.querySelectorAll('textarea.paragraph-input'))
    .find(x => (x.placeholder||'').indexOf('store prompts') < 0);
  if (!t) return 'NOSEL';
  t.focus(); t.value = p;
  t.dispatchEvent(new Event('input', {bubbles: true}));
  t.dispatchEvent(new Event('change', {bubbles: true}));
  return 'OK';
}"""


async def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "data/styles.json"
    only = [s.strip() for s in os.environ.get("ONLY", "").split("||") if s.strip()]
    captured = {}
    if only and os.path.exists(out):
        captured = json.load(open(out))
    async with async_playwright() as p:
        b = await p.firefox.launch(headless=True)
        ctx = await b.new_context(
            viewport={"width": 900, "height": 650},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0")
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        async def get_gen(pg):
            for _ in range(20):
                fr = [f for f in pg.frames if ".perchance.org/ai-text-to-image" in f.url
                      and "image-generation" not in f.url]
                if fr:
                    return fr[0]
                await pg.wait_for_timeout(500)
            return None

        pg = await ctx.new_page()
        holder = {}

        def on_req(req):
            if "/api/generate" in req.url and req.method == "POST" and req.post_data:
                try:
                    holder["body"] = json.loads(req.post_data)
                except Exception:
                    holder["body"] = {"prompt": req.post_data}

        pg.on("request", on_req)
        # Let the request reach the server (aborting trips the page's watchdog).
        await pg.goto(BASE_URL, timeout=90000)
        await pg.wait_for_timeout(7000)
        gen = await get_gen(pg)
        styles = await gen.evaluate("""() => {
          const sels = Array.from(document.querySelectorAll('select'));
          const sel = sels.find(s => Array.from(s.options).some(o => o.text.indexOf('Painted Anime') >= 0));
          return Array.from(sel.options).map(o => o.text.trim());
        }""")
        print(f"{len(styles)} styles found", flush=True)

        if only:
            styles = [s for s in styles if s in only]
            print(f"restricted to {len(styles)}: {styles}", flush=True)

        for i, name in enumerate(styles):
            try:
                gen = await get_gen(pg)
                if gen is None:
                    print("  lost frame, reloading", flush=True)
                    await pg.goto(BASE_URL, timeout=90000)
                    await pg.wait_for_timeout(7000)
                    gen = await get_gen(pg)
                holder.pop("body", None)
                res = await gen.evaluate(SET_STYLE, name)
                await gen.evaluate(SET_DESC, SENTINEL)
                await (await gen.query_selector('button:has-text("generate")')).click()
                for _ in range(40):
                    await pg.wait_for_timeout(250)
                    if "body" in holder:
                        break
                body = holder.get("body")
                if body and SENTINEL in body.get("prompt", ""):
                    captured[name] = {
                        "template": body["prompt"],
                        "negativePrompt": body.get("negativePrompt", ""),
                        "resolution": body.get("resolution"),
                        "guidanceScale": body.get("guidanceScale"),
                    }
                    print(f"[{i+1}/{len(styles)}] {name:28s} OK ({len(body['prompt'])} chars)", flush=True)
                else:
                    print(f"[{i+1}/{len(styles)}] {name:28s} MISS ({res})", flush=True)
                # let the in-flight generation settle a moment
                await pg.wait_for_timeout(600)
            except Exception as e:
                print(f"[{i+1}/{len(styles)}] {name:28s} ERR {type(e).__name__}: {str(e)[:60]}", flush=True)
                try:
                    await pg.goto(BASE_URL, timeout=90000)
                    await pg.wait_for_timeout(7000)
                except Exception:
                    pass

        json.dump(captured, open(out, "w"), indent=2)
        print(f"\nwrote {out} with {len(captured)}/{len(styles)} styles")
        await b.close()


asyncio.run(main())
