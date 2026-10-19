# Reverse-engineering scripts

These are **development-only** tools. The running service is pure HTTP and needs
none of them (`playwright` is not a runtime dependency and is not in the Docker
image).

They were used to discover the upstream protocol and to produce the committed
style catalog.

| Script | Needs | What it does |
|---|---|---|
| `probe.py` | Playwright + Firefox | Dumps every control (selects, textareas, buttons) on the live generator page. |
| `live_test.py` | Playwright + Firefox | Full browser round trip: sets controls, clicks generate, captures the image. Used to confirm the page behaviour. |
| `mem_test.py` | Playwright + Firefox | Measures PSS memory for tuned Firefox vs lean Chromium, to decide whether a browser fits a 512 MB host. |
| `http_probe.py` | `curl_cffi` | Checks whether a TLS-impersonating HTTP client can reach Perchance without a browser. |
| `api_probe.py` | Playwright + Firefox | Logs every `image-generation.perchance.org/api/*` call the page makes. |
| `flow_probe.py` | both | Captures the full request/response cycle, then replays it browserless. |
| `replay.py` | `curl_cffi` | Replays generate + download with a `userKey` only. |
| `key_age.py` | `curl_cffi` | Tests how long minted `userKey`s stay valid. |
| `extract_styles.py` | — | Parses the `t2i-styles` plugin out of the saved generator HTML. |
| `capture_styles.py` | Playwright + Firefox | **Produces `data/styles.json`**: resolves all 79 style templates by clicking generate with a sentinel description and reading the outgoing request. |

## Rebuilding the style catalog

Only necessary if Perchance changes its styles.

```bash
python3 -m playwright install firefox
python3 tools/capture_styles.py data/styles.json
```

Retry individual styles without redoing all of them:

```bash
ONLY='Cinematic||Flat Style Logo' python3 tools/capture_styles.py data/styles.json
```

## Findings worth knowing

* `/api/verifyUser?browserId=<32 hex>&thread=0` mints a `userKey` with plain HTTP
  — no Turnstile token, no `cf_clearance` cookie, no browser.
* `/api/generate` is a `POST` whose JSON body carries the prompt;
  `resolution` is `WxH` and `guidanceScale` defaults to 7.
* The image comes from `imageDownloadUrl` (returned in the same response) as a
  CORS-open `image/jpeg`.
* One in-flight generation per `userKey`; a second concurrent call returns
  `waiting_for_prev_request_to_finish`.
* The style dropdown is prompt injection: it expands a Perchance template around
  your description. `capture_styles.py` records the expanded result.
