"""Live end-to-end validation: controls, capture loop, memory, timing."""
import asyncio, base64, json, os, re, time

from playwright.async_api import async_playwright

BASE_URL = "https://perchance.org/ai-text-to-image-generator"
PROMPT = ("a lighthouse on a rocky coast at dusk, cinematic European hand-painted watercolour "
          "gouache, loose dark ink linework, visible brush strokes, textured paper, muted earthy "
          "palette, soft natural light, atmospheric, vintage animated film still")

NORM_JS = r"""
window.__norm = (s) => (s || '').normalize('NFKD').replace(/[^A-Za-z0-9]+/g, ' ').trim().toLowerCase();
window.__pickByStyle = (label) => {
  const sels = Array.from(document.querySelectorAll('select'));
  const styleSel = sels.find(s => Array.from(s.options).some(o => o.text.indexOf('Painted Anime') >= 0));
  if (!styleSel) return 'NOSEL';
  const opts = Array.from(styleSel.options);
  const want = window.__norm(label);
  let o = opts.find(x => window.__norm(x.text) === want)
       || opts.find(x => window.__norm(x.text).indexOf(want) >= 0);
  if (!o) return 'MISS';
  styleSel.selectedIndex = opts.indexOf(o);
  styleSel.dispatchEvent(new Event('input', {bubbles: true}));
  styleSel.dispatchEvent(new Event('change', {bubbles: true}));
  return 'OK:' + o.text;
};
window.__pickShape = (want) => {
  const sels = Array.from(document.querySelectorAll('select'));
  const sel = sels.find(s => Array.from(s.options).some(o => o.text.trim() === 'Landscape'));
  if (!sel) return 'NOSEL';
  const opts = Array.from(sel.options);
  const o = opts.find(x => window.__norm(x.text) === window.__norm(want));
  if (!o) return 'MISS';
  sel.selectedIndex = opts.indexOf(o);
  sel.dispatchEvent(new Event('input', {bubbles: true}));
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return 'OK:' + o.text;
};
window.__setDesc = (p) => {
  const tas = Array.from(document.querySelectorAll('textarea.paragraph-input'));
  const t = tas.find(x => (x.placeholder || '').indexOf('store prompts') < 0);
  if (!t) return 'NOSEL';
  t.focus(); t.value = p;
  t.dispatchEvent(new Event('input', {bubbles: true}));
  t.dispatchEvent(new Event('change', {bubbles: true}));
  return 'OK';
};
window.__findHidden = () => {
  const hits = [];
  document.querySelectorAll('input,textarea,select').forEach(el => {
    const blob = ((el.placeholder||'') + ' ' + (el.id||'') + ' ' + (el.name||'') + ' ' + (el.className||'')).toLowerCase();
    if (/negativ|seed|guidance|quality|steps|enhance|safe/.test(blob))
      hits.push({tag: el.tagName, type: el.type||null, id: el.id||null, placeholder: (el.placeholder||'').slice(0,80)});
  });
  return hits;
};
window.__imgSrcs = () => Array.from(document.querySelectorAll('img')).map(i => i.src);
"""


def firefox_rss_mb():
    total = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf8", "ignore")
            if "firefox" in cmd.lower() or "playwright" in cmd.lower():
                with open(f"/proc/{pid}/status") as fh:
                    for line in fh:
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1])
        except Exception:
            pass
    return round(total / 1024, 1)


async def main():
    t0 = time.time()
    report = {}
    async with async_playwright() as p:
        b = await p.firefox.launch(headless=True)
        report["launch_s"] = round(time.time() - t0, 1)
        report["rss_after_launch_mb"] = firefox_rss_mb()
        ctx = await b.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        pg = await ctx.new_page()
        await pg.goto(BASE_URL, timeout=90000)
        await pg.wait_for_timeout(7000)
        gen = [f for f in pg.frames
               if ".perchance.org/ai-text-to-image" in f.url and "image-generation" not in f.url][0]
        report["rss_after_load_mb"] = firefox_rss_mb()
        await gen.evaluate(NORM_JS)

        report["hidden_controls"] = await gen.evaluate("() => window.__findHidden()")
        report["style_no_style"] = await gen.evaluate("() => window.__pickByStyle('No style')")
        report["style_watercolor"] = await gen.evaluate("() => window.__pickByStyle('Watercolor')")
        report["shape"] = await gen.evaluate("() => window.__pickShape('Landscape')")
        report["desc"] = await gen.evaluate("(p) => window.__setDesc(p)", PROMPT)

        # count select -> 2
        report["count"] = await gen.evaluate("""() => {
          const sels = Array.from(document.querySelectorAll('select'));
          const s = sels.find(x => { const t = Array.from(x.options).map(o=>o.text.trim()); return t.includes('2') && t.length <= 8; });
          if (!s) return 'NOSEL';
          const o = Array.from(s.options).find(x => x.text.trim() === '2');
          s.selectedIndex = Array.from(s.options).indexOf(o);
          s.dispatchEvent(new Event('input', {bubbles: true}));
          s.dispatchEvent(new Event('change', {bubbles: true}));
          return 'OK';
        }""")

        baseline = set()
        for f in pg.frames:
            if "image-generation.perchance.org/embed" in f.url:
                baseline.update(await f.evaluate("() => window.__imgSrcs ? window.__imgSrcs() : Array.from(document.querySelectorAll('img')).map(i=>i.src)"))

        report["frames_before_gen"] = [f.url[:90] for f in pg.frames]
        t_click = time.time()
        await (await gen.query_selector('button:has-text("generate")')).click()

        got = []
        deadline = time.time() + 300
        while time.time() < deadline and len(got) < 2:
            await pg.wait_for_timeout(2000)
            for f in pg.frames:
                if "image-generation.perchance.org/embed" not in f.url:
                    continue
                try:
                    items = await f.evaluate(
                        "() => Array.from(document.querySelectorAll('img')).map(i=>({s:i.src,w:i.naturalWidth,h:i.naturalHeight}))")
                except Exception:
                    continue
                for it in items:
                    if it["s"].startswith("data:image") and it["w"] > 200 and it["s"] not in baseline:
                        if it["s"] not in [g["s"] for g in got]:
                            got.append(it)
            if got:
                report["first_image_s"] = round(time.time() - t_click, 1)

        report["images"] = [{"w": g["w"], "h": g["h"], "bytes": len(g["s"]) * 3 // 4} for g in got]
        report["frames_after_gen"] = sorted({f.url[:90] for f in pg.frames})
        report["rss_after_gen_mb"] = firefox_rss_mb()
        if got:
            raw = base64.b64decode(got[0]["s"].split(",", 1)[1])
            open("/tmp/opencode/live_test.jpg", "wb").write(raw)
            report["saved_bytes"] = len(raw)
        report["total_s"] = round(time.time() - t0, 1)
        await b.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))


asyncio.run(main())
