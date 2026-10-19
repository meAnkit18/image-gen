"""How long does a userKey stay valid? Test keys captured at known ages."""
import json, time
from curl_cffi import requests as cr

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
BASE = "https://image-generation.perchance.org"

# (label, approx age when captured)
KEYS = [
    ("flow_probe  ~20min ago", "5f117d568b166cf3989d33fecaa0573630802db3c6e04d23b7c4c284c0e88cb4"),
    ("api_probe   ~35min ago", "0f5cf3c032ddc5cd9b48cf753cb0cb8257fc82ab40285088f180b26876c20c95"),
    ("live_probe  ~65min ago", "6f6c38b1026950a746fdfd4968f7d4c6f2b6e8d91a18705b79556b02fcb78498"),
    ("garbage key",            "0" * 64),
]

s = cr.Session(impersonate="firefox")
hdr = {"user-agent": UA, "referer": "https://image-generation.perchance.org/embed",
       "accept": "*/*", "content-type": "text/plain;charset=UTF-8"}
body = {"prompt": "a small wooden boat on calm water", "negativePrompt": "", "seed": -1,
        "resolution": "768x512", "guidanceScale": 7, "channel": "ai-text-to-image-generator",
        "subChannel": "public", "adAccessCode": "", "requestId": "1"}

for label, key in KEYS:
    body["userKey"] = key
    r = s.post(f"{BASE}/api/generate?userKey={key}&requestId={time.time()}&adAccessCode=&__cacheBust={time.time()}",
               data=json.dumps(body), headers=hdr, timeout=60)
    try:
        st = r.json().get("status")
    except Exception:
        st = f"<{r.status_code} {r.text[:60]}>"
    print(f"  {label:24s} -> {st}")

print("\n=== checkUserVerificationStatus ===")
for label, key in KEYS[:3]:
    r = s.get(f"{BASE}/api/checkUserVerificationStatus?userKey={key}&cacheKey={int(time.time()/3)}",
              headers=hdr, timeout=30)
    print(f"  {label:24s} -> {r.status_code} {r.text[:160]}")
