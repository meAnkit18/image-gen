# Perchance Image Generator API

A small, self-hostable HTTP API that generates images with
[Perchance](https://perchance.org/ai-text-to-image-generator) and returns them
directly in the response body — `POST` a prompt, get a JPEG back.

It also speaks **MCP** (Model Context Protocol) over Streamable HTTP, so agent
clients can call it as a tool.

**Designed for Render's free tier**: ~55 MB RSS, no browser, no API key, no login.

```bash
curl -X POST https://your-app.onrender.com/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red lighthouse on a rocky coast","style":"Watercolor","shape":"landscape"}' \
  --output lighthouse.jpg
```

---

## Why this is small (and how it was built)

The reference approach for this site drives a headless Firefox via Playwright and
scrapes inline base64 pixels from nested iframes. That works, but it measures at
**~660 MB PSS** — over Render's free 512 MB limit, before you even count the
Playwright driver.

While building this, the upstream protocol was traced end to end and the browser
turned out to be unnecessary. The whole pipeline is plain HTTP:

```
GET  /api/verifyUser?browserId=<32 hex>&thread=0
     -> {"status":"success","userKey":"<64 hex>"}

POST /api/generate?userKey=<key>&requestId=<n>&adAccessCode=
     body: {"prompt","negativePrompt","seed","resolution","guidanceScale",
            "channel","subChannel","userKey","adAccessCode","requestId"}
     -> {"status":"success","imageDownloadUrl":"/api/downloadTemporaryImageViaProxy?t=...",
         "seed":...,"width":...,"height":...,"guidanceScale":...}

GET  <imageDownloadUrl>   -> image/jpeg
```

A fresh `browserId` mints a `userKey` with no CAPTCHA and no cookie, so the
service never needs a browser. A quick measurement inside the real Docker image,
throttled to 512 MB / 0.1 CPU:

| | Browser approach | This service |
|---|---|---|
| Peak memory | ~660 MB | **~54 MB** |
| Image latency | 15–40 s | **7–10 s** |
| Render free tier | OOM | fits with ~90% headroom |

Two upstream behaviours shape the code:

* **One in-flight generation per `userKey`.** Firing two at once answers
  `{"status":"waiting_for_prev_request_to_finish"}`. Rather than serialise,
  `KEY_POOL_SIZE` independent identities are kept — one per parallel slot.
* **Keys are long-lived.** Keys were still valid (`{"status":"verified"}`) over
  an hour after minting, so they are cached and refreshed automatically if
  Perchance ever returns `invalid_key`.

Style handling is exact rather than approximate: Perchance expands a template
around your description (supporting `{a|b}` choices and cross-plugin variables),
so `tools/capture_styles.py` captured the *resolved* template for all **79
styles** once and stored it in `data/styles.json`. The runtime just substitutes
your prompt into it.

---

## Quick start (local)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000/docs> for interactive OpenAPI docs, then:

```bash
curl -X POST http://127.0.0.1:8000/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a tiny cabin in a snowy pine forest","style":"Painterly","shape":"landscape"}' \
  --output cabin.jpg
```

No configuration is required. Keys are minted on first use.

## Deploy to Render

### Blueprint (recommended)

1. Push this repository to GitHub.
2. Render → **New +** → **Blueprint** → pick the repo. `render.yaml` is detected.
3. Deploy. Render generates an `API_KEY` for you — copy it from the service's
   **Environment** tab.

### Manual Docker service

| Setting | Value |
|---|---|
| Runtime | Docker |
| Plan | Free |
| Dockerfile path | `./Dockerfile` |
| Health check path | `/healthz` |

Then set the environment variables you want (see [Configuration](#configuration)).

### Free-tier reality check

* **512 MB / 0.1 CPU.** This service uses ~55 MB, so that is not a problem.
* **Sleeps after 15 idle minutes** and cold-starts in ~60 s. The first request
  after a quiet period includes that wait. Point a free uptime monitor
  (UptimeRobot, cron-job.org) at `/healthz` every 10–14 min to keep it warm.
* **750 instance hours/month.** Running 24/7 is ~730 h, which fits — but only
  just, and only for one service.
* **No persistent disk.** Nothing is stored locally, so nothing is lost on
  restart. Styles ship inside the image; generation output is returned, not
  saved.
* **One instance only.** The key pool and rate limiter are per-process.

---

## API

Base URL is your deployment (or `http://127.0.0.1:8000` locally).

### `GET /v1/options`

The full parameter catalog: every field, its type, default, range, the shape →
resolution map, presets and worked examples. Start here.

### `GET /v1/styles`

All 79 upstream styles. `?include_templates=true` additionally returns the exact
prompt template each style wraps around your description.

```json
{
  "count": 79,
  "styles": [
    {"name": "𝗡𝗼 𝘀𝘁𝘆𝗹𝗲", "id": "no-style", "no_style": true, "negative_prompt": ""},
    {"name": "Watercolor", "id": "watercolor", "no_style": false, "negative_prompt": ""}
  ]
}
```

### `POST /v1/generate`

```json
{
  "prompt": "a red lighthouse on a rocky coast at dusk",
  "style": "Watercolor",
  "shape": "landscape"
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `prompt` | string | — | **Required.** What to draw. |
| `style` | string | `No style` | Any `name` or `id` from `/v1/styles`. Unknown values fall back to no style. |
| `preset` | string | — | `coloring_page` or `cinematic_watercolor`. Fills style/shape/suffix. |
| `suffix` | string | — | Text appended to the prompt. |
| `negative_prompt` | string | — | Merged with any negative prompt the style defines. |
| `shape` | string | `square` | `square` 768×768, `portrait` 512×768, `landscape` 768×512. |
| `resolution` | string | from shape | Explicit `"WxH"` (256–1536, multiples of 8). Wins over `shape`. |
| `seed` | int | `-1` | `-1`/omitted = random. The seed used comes back in headers and metadata. |
| `guidance_scale` | number | `7` | 1–20. |
| `count` | int | `1` | 1–`MAX_COUNT` (8 by default). Each image is a separate generation. |
| `response_format` | string | `image` | `image`, `zip` or `json`. |

**Responses**

| `response_format` | `count` | Result |
|---|---|---|
| `image` | 1 | `image/jpeg` body |
| `image` | >1 | `application/zip` (images + `metadata.json`) |
| `zip` | any | `application/zip` |
| `json` | any | JSON with base64 payloads |

Useful response headers: `X-Request-Id`, `X-Image-Seed`, `X-Generation-Ms`.

```bash
# Four coloring pages as a zip
curl -X POST http://127.0.0.1:8000/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a smiling baby cow","preset":"coloring_page","count":4,"response_format":"zip"}' \
  --output pages.zip

# Reproducible image + metadata as JSON
curl -X POST http://127.0.0.1:8000/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"an airline pilot crossing a misty apron","preset":"cinematic_watercolor","seed":123456,"response_format":"json"}'
```

### Ops endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness. No upstream call. Use for Render's health check and keep-alive pings. |
| `GET /readyz` | Readiness. Verifies the upstream key is still `verified`. |
| `GET /v1/stats` | Generation counters (respects auth). |
| `GET /docs` | Interactive OpenAPI docs (disable with `EXPOSE_DOCS=false`). |

### MCP

Mounted at `POST /mcp` (Streamable HTTP, stateless, JSON responses).

```json
{
  "mcpServers": {
    "perchance": {
      "type": "http",
      "url": "https://your-app.onrender.com/mcp",
      "headers": { "X-API-Key": "your-key" }
    }
  }
}
```

Tools: `get_options`, `list_styles`, `service_status`, `generate_image`.
`generate_image` returns the picture as real MCP `image` content.

---

## Authentication

Auth is **optional** and off unless `API_KEY` is set. When set, send either:

```
X-API-Key: <key>
Authorization: Bearer <key>
```

`/healthz` and `/readyz` stay open so platform health checks and uptime monitors
work without a key. Rate limiting is per client IP.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | *(unset)* | Enables auth when set. |
| `MAX_COUNT` | `8` | Maximum `count` per request (hard cap 32). |
| `MAX_PROMPT_CHARS` | `2500` | Advisory prompt length limit. |
| `KEY_POOL_SIZE` | `3` | Upstream identities = max parallel generations. |
| `RATE_LIMIT_PER_MINUTE` | `30` | Per-IP limit; `0` disables. |
| `DEFAULT_STYLE` | `No style` | Style when the request omits one. |
| `DEFAULT_SHAPE` | `square` | Shape when the request omits one. |
| `GENERATE_DEADLINE` | `240` | Seconds before a generation is abandoned. |
| `UPSTREAM_TIMEOUT` | `120` | Per-HTTP-call timeout. |
| `PERCHANCE_USER_KEY` | *(unset)* | Pin a specific upstream key instead of minting. |
| `PERCHANCE_BROWSER_ID` | *(unset)* | Identity paired with `PERCHANCE_USER_KEY`. |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins. |
| `EXPOSE_DOCS` | `true` | Serve `/docs` and `/openapi.json`. |
| `ENABLE_MCP` | `true` | Mount `/mcp`. |
| `MCP_ALLOWED_HOSTS` | *(unset)* | Comma-separated Host values; enables MCP DNS-rebinding checks. |
| `LOG_LEVEL` | `info` | Python log level. |
| `PORT` | `8080` | Bind port (Render sets this). |

---

## Project layout

```
app/
  config.py     environment-driven settings
  perchance.py  upstream client, key pool, retry/refresh logic
  models.py     request schemas + style/preset resolution
  styles.py     style catalog lookup (case/Unicode/alias tolerant)
  presets.py    coloring-page and cinematic-watercolour recipes
  ratelimit.py  in-memory sliding window
  main.py       FastAPI routes, auth, error handling
  mcp.py        MCP server (mounted at /mcp)
  state.py      process-wide client singleton
data/
  styles.json   79 styles with their real resolved prompt templates
tools/          one-off reverse-engineering and capture scripts
tests/          offline unit tests
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

27 tests, no network: the upstream client is stubbed.

## Regenerating `data/styles.json`

Only needed if Perchance changes its style list. Requires Playwright + Firefox
locally (not needed at runtime):

```bash
python3 -m playwright install firefox
python3 tools/capture_styles.py data/styles.json
```

## Caveats

* This is an **unofficial wrapper around an undocumented endpoint**. It can break
  if Perchance changes its API, and it is not affiliated with or endorsed by
  Perchance. Check their terms before running heavy workloads.
* Upstream moderation is enforced by Perchance, not by this service. Images are
  returned as-is; `maybe_nsfw` is surfaced from the upstream response.
* The free tier is a single point of failure with no SLA. For anything serious,
  pin a paid instance and keep `KEY_POOL_SIZE` modest.
