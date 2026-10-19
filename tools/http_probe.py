"""Test whether curl_cffi (TLS/JA3 impersonation) can reach Perchance without a browser."""
from curl_cffi import requests as cr

FF_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
CH_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/131.0.0.0 Safari/537.36")

TESTS = [
    ("homepage", "https://perchance.org/ai-text-to-image-generator", FF_UA),
    ("embed", "https://image-generation.perchance.org/embed", FF_UA),
    ("verifyUser", "https://image-generation.perchance.org/api/verifyUser?thread=0", FF_UA),
]

for imp in ("firefox", "chrome", "chrome136"):
    print(f"\n===== impersonate={imp} =====")
    s = cr.Session(impersonate=imp)
    for name, url, ua in TESTS:
        try:
            r = s.get(url, headers={"user-agent": ua, "accept": "*/*",
                                    "referer": "https://image-generation.perchance.org/embed"},
                      timeout=30)
            body = r.text[:180].replace("\n", " ")
            print(f"  {name:12s} {r.status_code:4d}  len={len(r.text):7d}  {body[:120]}")
            print(f"               cookies={list(s.cookies.keys())}")
        except Exception as e:
            print(f"  {name:12s} EXC {type(e).__name__}: {str(e)[:100]}")
