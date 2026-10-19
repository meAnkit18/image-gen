"""Memory-tuned variant test. Measures PSS (shared-page aware) for firefox + node driver.

Usage: python3 tools/mem_test.py [firefox-opt|firefox-plain]
"""
import asyncio, base64, json, os, sys, time

from playwright.async_api import async_playwright

BASE_URL = "https://perchance.org/ai-text-to-image-generator"
PROMPT = ("a lighthouse on a rocky coast at dusk, cinematic European hand-painted watercolour "
          "gouache, loose dark ink linework, visible brush strokes, textured paper, muted earthy "
          "palette, soft natural light, atmospheric, vintage animated film still")

BLOCK = [
    "*ad-sandbox*", "*doubleclick*", "*googlesyndication*", "*google-analytics*",
    "*googletagmanager*", "*facebook.net*", "*amazon-adsystem*", "*criteo*",
    "*taboola*", "*outbrain*", "*pubmatic*", "*rubiconproject*", "*casalemedia*",
]

# Firefox memory-cutting prefs. The big one is collapsing e10s to 1 content process.
FF_PREFS = {
    "dom.ipc.processCount": 1,
    "dom.ipc.processCount.webIsolated": 1,
    "dom.ipc.processCount.extension": 1,
    "dom.ipc.processCount.file": 1,
    "dom.ipc.processCount.privilegedabout": 1,
    "dom.ipc.processCount.privilegedmozilla": 1,
    "browser.tabs.remote.autostart": False,
    "browser.tabs.remote.autostart.2": False,
    "media.ffmpeg.disabled": True,
    "media.peerconnection.enabled": False,
    "media.autoplay.default": 5,
    "media.hardware-video-decoding.enabled": False,
    "media.webspeech.recognition.enable": False,
    "gfx.webrender.all": False,
    "gfx.webrender.software": True,
    "layers.acceleration.disabled": True,
    "image.mem.max_decoded_image_kb": 65536,
    "image.mem.surfacecache.max_size_kb": 65536,
    "browser.cache.memory.capacity": 16384,
    "browser.sessionhistory.max_entries": 2,
    "browser.sessionhistory.max_total_viewers": 0,
    "browser.privatebrowsing.autostart": True,
    "network.prefetch-next": False,
    "network.dns.disablePrefetch": True,
    "network.http.speculative-parallel-limit": 0,
    "security.OCSP.enabled": 0,
    "beacon.enabled": False,
    "dom.battery.enabled": False,
    "extensions.pocket.enabled": False,
    "extensions.formautofill.addresses.enabled": False,
    "toolkit.telemetry.enabled": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "javascript.options.mem.high_water_mark": 32,
    "javascript.options.mem.gc_allocation_threshold_mb": 20,
    "browser.cache.disk.enable": False,
}


def proc_group_pss():
    """PSS in MB for browser processes and the playwright node driver."""
    ff = node = py = 0.0
    nproc = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf8", "ignore").replace("\x00", " ")
            kind = None
            # Only count browsers Playwright launched from its own cache dir,
            # never unrelated system browsers (e.g. the user's desktop Chrome).
            if "ms-playwright" in cmd and ("firefox" in cmd or "chrome" in cmd or "headless_shell" in cmd):
                kind = "ff"
                nproc += 1
            elif "playwright" in cmd and "node" in cmd:
                kind = "node"
            elif "python3" in cmd and "mem_test" in cmd:
                kind = "py"
            if not kind:
                continue
            with open(f"/proc/{pid}/smaps_rollup") as fh:
                for line in fh:
                    if line.startswith("Pss:"):
                        val = int(line.split()[1]) / 1024
                        if kind == "ff":
                            ff += val
                        elif kind == "node":
                            node += val
                        else:
                            py += val
                        break
        except Exception:
            pass
    return {"firefox": round(ff, 1), "node": round(node, 1), "python": round(py, 1),
            "procs": nproc, "total": round(ff + node + py, 1)}


CHROMIUM_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
    "--disable-gpu", "--disable-software-rasterizer", "--single-process",
    "--no-zygote", "--disable-extensions", "--disable-background-networking",
    "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter",
    "--js-flags=--max-old-space-size=192",
    "--mute-audio", "--hide-scrollbars", "--disable-sync", "--no-first-run",
]


async def main():
    variant = sys.argv[1] if len(sys.argv) > 1 else "firefox-opt"
    opt = variant in ("firefox-opt", "firefox-ultra")
    ultra = variant == "firefox-ultra"
    chromium = variant.startswith("chromium")
    t0 = time.time()
    r = {"variant": variant}
    async with async_playwright() as p:
        if chromium:
            b = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        else:
            kw = {"headless": True}
            if opt:
                kw["firefox_user_prefs"] = FF_PREFS
            b = await p.firefox.launch(**kw)
        r["pss_after_launch"] = proc_group_pss()
        ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36") if chromium else \
             "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
        ctx = await b.new_context(
            viewport={"width": 800, "height": 600} if opt else {"width": 1400, "height": 900},
            user_agent=ua,
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        pg = await ctx.new_page()
        if opt:
            if ultra:
                async def handler(route):
                    req = route.request
                    if req.resource_type in ("image", "media", "font"):
                        return await route.abort()
                    if any(k.strip("*") in req.url for k in BLOCK):
                        return await route.abort()
                    return await route.continue_()
                await pg.route("**/*", handler)
            else:
                await pg.route("**/*", lambda route: route.abort()
                               if any(k.strip("*") in route.request.url for k in BLOCK)
                               else route.continue_())
        await pg.goto(BASE_URL, timeout=90000)
        await pg.wait_for_timeout(7000)
        r["pss_after_load"] = proc_group_pss()
        gen = [f for f in pg.frames
               if ".perchance.org/ai-text-to-image" in f.url and "image-generation" not in f.url][0]

        await gen.evaluate("""() => {
          const norm = (s) => (s||'').normalize('NFKD').replace(/[^A-Za-z0-9]+/g,' ').trim().toLowerCase();
          const sels = Array.from(document.querySelectorAll('select'));
          const set = (sel, want) => {
            if (!sel) return 'NOSEL';
            const opts = Array.from(sel.options);
            const w = norm(want);
            const o = opts.find(x => norm(x.text) === w) || opts.find(x => norm(x.text).indexOf(w) >= 0);
            if (!o) return 'MISS';
            sel.selectedIndex = opts.indexOf(o);
            sel.dispatchEvent(new Event('input', {bubbles:true}));
            sel.dispatchEvent(new Event('change', {bubbles:true}));
            return 'OK';
          };
          const styleSel = sels.find(s => Array.from(s.options).some(o => o.text.indexOf('Painted Anime') >= 0));
          const shapeSel = sels.find(s => Array.from(s.options).some(o => o.text.trim() === 'Landscape'));
          const tas = Array.from(document.querySelectorAll('textarea.paragraph-input'));
          const desc = tas.find(x => (x.placeholder||'').indexOf('store prompts') < 0);
          window.__r = [
            set(styleSel, 'Watercolor'),
            set(shapeSel, 'Landscape'),
            (desc ? 'OK' : 'NOSEL'),
          ];
          if (desc) { desc.focus(); desc.value = window.__p;
            desc.dispatchEvent(new Event('input',{bubbles:true}));
            desc.dispatchEvent(new Event('change',{bubbles:true})); }
          return window.__r;
        }""")
        await gen.evaluate("(p) => { const t = Array.from(document.querySelectorAll('textarea.paragraph-input')).find(x => (x.placeholder||'').indexOf('store prompts') < 0); t.focus(); t.value = p; t.dispatchEvent(new Event('input',{bubbles:true})); t.dispatchEvent(new Event('change',{bubbles:true})); }", PROMPT)

        baseline = set()
        for f in pg.frames:
            if "image-generation.perchance.org/embed" in f.url:
                try:
                    baseline.update(await f.evaluate("() => Array.from(document.querySelectorAll('img')).map(i=>i.src)"))
                except Exception:
                    pass
        t_click = time.time()
        await (await gen.query_selector('button:has-text("generate")')).click()
        got = []
        deadline = time.time() + 300
        while time.time() < deadline and not got:
            await pg.wait_for_timeout(2000)
            for f in pg.frames:
                if "image-generation.perchance.org/embed" not in f.url:
                    continue
                try:
                    items = await f.evaluate("() => Array.from(document.querySelectorAll('img')).map(i=>({s:i.src,w:i.naturalWidth}))")
                except Exception:
                    continue
                for it in items:
                    if it["s"].startswith("data:image") and it["w"] > 200 and it["s"] not in baseline:
                        got.append(it)
        r["ok"] = bool(got)
        r["first_image_s"] = round(time.time() - t_click, 1)
        r["pss_after_gen"] = proc_group_pss()
        if got:
            open(f"/tmp/opencode/mem_{variant}.jpg", "wb").write(
                base64.b64decode(got[0]["s"].split(",", 1)[1]))
        r["total_s"] = round(time.time() - t0, 1)
        await b.close()
    print(json.dumps(r, indent=2))


asyncio.run(main())
