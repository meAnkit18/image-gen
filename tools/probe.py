"""Probe the live Perchance page and dump every available control as JSON.

Usage:
    env -u PYTHONHOME -u PYTHONPATH python3 tools/probe.py [outfile.json]
"""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

BASE_URL = "https://perchance.org/ai-text-to-image-generator"

PROBE_JS = r"""() => {
  const out = {selects: [], textareas: [], inputs: [], buttons: [], checks: []};
  document.querySelectorAll('select').forEach((s, i) => {
    out.selects.push({
      index: i,
      id: s.id || null,
      name: s.name || null,
      cls: s.className || null,
      selectedIndex: s.selectedIndex,
      selectedText: s.options[s.selectedIndex] ? s.options[s.selectedIndex].text.trim() : null,
      options: Array.from(s.options).map(o => o.text.trim()),
    });
  });
  document.querySelectorAll('textarea').forEach((t, i) => {
    out.textareas.push({
      index: i, id: t.id || null, name: t.name || null,
      cls: t.className || null, placeholder: t.placeholder || null,
      value: (t.value || '').slice(0, 120),
    });
  });
  document.querySelectorAll('input').forEach((el, i) => {
    out.inputs.push({
      index: i, type: el.type || null, id: el.id || null, name: el.name || null,
      placeholder: el.placeholder || null, value: (el.value || '').slice(0, 80),
      cls: el.className || null,
    });
  });
  document.querySelectorAll('button').forEach((el, i) => {
    const txt = (el.textContent || '').trim().replace(/\s+/g, ' ');
    if (txt) out.buttons.push({index: i, text: txt.slice(0, 60), id: el.id || null});
  });
  document.querySelectorAll('input[type=checkbox], input[type=radio]').forEach((el) => {
    out.checks.push({id: el.id || null, name: el.name || null, checked: el.checked, label: el.labels && el.labels[0] ? el.labels[0].innerText.trim().slice(0,60) : null});
  });
  return out;
}"""


async def main():
    async with async_playwright() as p:
        b = await p.firefox.launch(headless=True)
        ctx = await b.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        pg = await ctx.new_page()
        await pg.goto(BASE_URL, timeout=90000)
        await pg.wait_for_timeout(8000)

        frames = [f.url for f in pg.frames]
        gens = [
            f
            for f in pg.frames
            if ".perchance.org/ai-text-to-image" in f.url and "image-generation" not in f.url
        ]
        result = {"frame_urls": frames, "gen_frames": [f.url for f in gens], "controls": None}
        if gens:
            result["controls"] = await gens[0].evaluate(PROBE_JS)
        print(json.dumps(result, indent=2))
        out = sys.argv[1] if len(sys.argv) > 1 else None
        if out:
            with open(out, "w") as fh:
                json.dump(result, fh, indent=2)
        await b.close()


asyncio.run(main())
