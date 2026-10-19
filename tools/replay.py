"""Browserless replay test: can curl_cffi generate + download with just a userKey?"""
import json, time

from curl_cffi import requests as cr

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
USERKEY = "5f117d568b166cf3989d33fecaa0573630802db3c6e04d23b7c4c284c0e88cb4"
PROMPT = "a red lighthouse on a cliff at dusk, muted watercolour painting, loose ink lines"
BASE = "https://image-generation.perchance.org"

s = cr.Session(impersonate="firefox")
hdr = {"user-agent": UA, "referer": "https://image-generation.perchance.org/embed",
       "accept": "*/*", "content-type": "text/plain;charset=UTF-8"}

body = {
    "prompt": PROMPT,
    "negativePrompt": "",
    "seed": -1,
    "resolution": "768x512",
    "guidanceScale": 7,
    "channel": "ai-text-to-image-generator",
    "subChannel": "public",
    "userKey": USERKEY,
    "adAccessCode": "",
    "requestId": str(time.time()),
}

t0 = time.time()
for attempt in range(6):
    rid = f"{time.time()}"
    url = f"{BASE}/api/generate?userKey={USERKEY}&requestId={rid}&adAccessCode=&__cacheBust={time.time()}&bdf={time.time()}"
    r = s.post(url, data=json.dumps(body), headers=hdr, timeout=180)
    try:
        j = r.json()
    except Exception:
        print("non-json:", r.status_code, r.text[:200]); break
    st = j.get("status")
    print(f"attempt {attempt}: http={r.status_code} status={st!r} "
          f"keys={list(j.keys())}")
    if st == "success":
        print("  imageId:", j.get("imageId"))
        print("  imageDownloadUrl:", str(j.get("imageDownloadUrl"))[:90], "...")
        print("  w/h:", j.get("width"), j.get("height"), "seed:", j.get("seed"))
        dd = s.get(BASE + j["imageDownloadUrl"], headers=hdr, timeout=90)
        print(f"  DOWNLOAD http={dd.status_code} ct={dd.headers.get('content-type')} "
              f"bytes={len(dd.content)} jpeg={dd.content[:2] == b'\xff\xd8'}")
        if dd.content[:2] == b"\xff\xd8":
            open("/tmp/opencode/replay.jpg", "wb").write(dd.content)
            print("  saved /tmp/opencode/replay.jpg")
        # second back-to-back generation to test key reuse
        r2 = s.post(f"{BASE}/api/generate?userKey={USERKEY}&requestId={time.time()}&adAccessCode=&__cacheBust={time.time()}",
                    data=json.dumps({**body, "prompt": "a blue sailboat, muted watercolour"}), headers=hdr, timeout=180)
        print("  2nd gen:", r2.json().get("status"))
        break
    if st == "invalid_key":
        print("  -> userKey rejected"); break
    time.sleep(3)
print(f"total {time.time()-t0:.1f}s")
