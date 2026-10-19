"""Capture the full Perchance generation flow, then try replaying it browserless.

Phase 1 (browser): capture userKey, the exact POST /api/generate body, the full
generate response, and how the image bytes are fetched.
Phase 2 (curl_cffi): replay generate + download with no browser at all.
"""
import asyncio, json, time

from curl_cffi import requests as cr
from playwright.async_api import async_playwright

BASE_URL = "https://perchance.org/ai-text-to-image-generator"
PROMPT = "a red lighthouse on a cliff at dusk, watercolour painting"
CAP = {"req": [], "gen_resp": None, "userkey": None, "gen_body": None,
       "gen_url": None, "dl": [], "frame_of": {}}


async def phase1():
    async with async_playwright() as p:
        b = await p.firefox.launch(headless=True)
        ctx = await b.new_context(
            viewport={"width": 900, "height": 650},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0")
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        async def on_resp(resp):
            u = resp.url
            if "image-generation.perchance.org" not in u:
                return
            if "/api/verifyUser" in u:
                try:
                    CAP["userkey"] = json.loads(await resp.text()).get("userKey") or CAP["userkey"]
                except Exception:
                    pass
            if "/api/generate" in u:
                try:
                    j = json.loads(await resp.text())
                except Exception:
                    j = {"unreadable": True}
                if j.get("status") == "success":
                    CAP["gen_resp"] = {k: (str(v)[:200] + "…" if k == "prompt" else v)
                                       for k, v in j.items()}
            if "downloadTemporaryImage" in u:
                CAP["dl"].append({"url": u[:200], "status": resp.status,
                                  "ct": resp.headers.get("content-type"),
                                  "len": resp.headers.get("content-length")})

        def on_req(req):
            u = req.url
            if "image-generation.perchance.org" not in u:
                return
            if "/api/generate" in u and req.method == "POST" and req.post_data:
                CAP["gen_url"] = u
                CAP["gen_body"] = req.post_data

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
        }""", PROMPT)
        await (await gen.query_selector('button:has-text("generate")')).click()
        for _ in range(60):
            await pg.wait_for_timeout(2000)
            if CAP["gen_resp"]:
                break
        await pg.wait_for_timeout(6000)
        # where do the displayed images come from?
        CAP["img_srcs"] = []
        for f in pg.frames:
            if "image-generation.perchance.org/embed" in f.url:
                try:
                    CAP["img_srcs"] = await f.evaluate(
                        "() => Array.from(document.querySelectorAll('img')).map(i=>({src:i.src.slice(0,40), w:i.naturalWidth}))")
                except Exception:
                    pass
        await b.close()


def phase2():
    out = {}
    ua = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    s = cr.Session(impersonate="firefox")
    hdr = {"user-agent": ua, "referer": "https://image-generation.perchance.org/embed",
           "accept": "*/*", "content-type": "text/plain;charset=UTF-8"}
    body = CAP["gen_body"]
    # strip the request-scoped query, rebuild with a fresh requestId
    url = CAP["gen_url"].split("&requestId=")[0] if CAP["gen_url"] else None
    if not url:
        out["error"] = "no captured generate url"
        return out
    out["generate_url"] = url
    for attempt in range(4):
        rid = f"0.{int(time.time()*1000)%1000000}"
        r = s.post(f"{url}&requestId={rid}&__cacheBust={time.time()}", data=body, headers=hdr, timeout=120)
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text[:200]}
        out[f"generate_attempt{attempt}"] = {"status": r.status_code, "resp": {k: (str(v)[:80] if k == "prompt" else v) for k, v in j.items()}}
        if j.get("status") == "success":
            img_id = j.get("imageId")
            out["imageId"] = img_id
            # try direct download
            for dlname, dlurl in [
                ("downloadTemporaryImage", f"https://image-generation.perchance.org/api/downloadTemporaryImage?imageId={img_id}"),
            ]:
                d = s.get(dlurl, headers=hdr, timeout=60)
                out[dlname] = {"status": d.status, "ct": d.headers.get("content-type"),
                               "bytes": len(d.content)}
                if d.status == 200 and d.content[:2] == b"\xff\xd8":
                    open("/tmp/opencode/replay.jpg", "wb").write(d.content)
                    out[dlname]["saved"] = True
            break
        time.sleep(2)
    return out


async def main():
    await phase1()
    print("=== PHASE 1 (browser capture) ===")
    print(json.dumps({k: v for k, v in CAP.items() if k != "gen_body"}, indent=2)[:2500])
    if CAP["gen_body"]:
        try:
            jb = json.loads(CAP["gen_body"])
            jb["prompt"] = jb["prompt"][:120] + f"... <{len(jb.get('prompt',''))} chars>"
            print("gen_body keys/values:", json.dumps(jb, indent=2))
        except Exception:
            print("gen_body raw:", CAP["gen_body"][:400])
    print("\n=== PHASE 2 (browserless replay) ===")
    print(json.dumps(phase2(), indent=2))


asyncio.run(main())
