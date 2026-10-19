"""Extract every Perchance t2i style and its prompt suffix template.

The generator page embeds the t2i-styles plugin source as a JSON string. Styles
are declared as Perchance `$output` blocks:

    $output
      Painted Anime
        prompt // comment
          [input.description], art in the style of ...

Usage: python3 tools/extract_styles.py [gen.html] [out.json]
"""
import json, re, sys

src = open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/gen.html").read()

m = re.search(r'\{"name":"t2i-styles","modelText":"((?:[^"\\]|\\.)*)"', src, re.S)
if not m:
    sys.exit("t2i-styles plugin not found")
model = json.loads(f'"{m.group(1)}"')
print(f"t2i-styles source: {len(model)} chars")

# Grab everything after the first $output up to the next top-level plugin boundary.
body = model.split("$output", 1)[1]

styles = {}
order = []
cur = None
for raw in body.split("\n"):
    if raw.strip().startswith("//"):
        continue
    stripped = raw.strip()
    if not stripped:
        continue
    indent = len(raw) - len(raw.lstrip())
    # A style name sits at 2-space indent and is not a key like "prompt"
    if indent == 2 and stripped not in ("prompt", "negativePrompt", "description", "seed", "resolution"):
        cur = stripped
        if cur not in styles:
            styles[cur] = {"suffix": "", "comment": ""}
            order.append(cur)
        continue
    if cur and indent == 4 and stripped.startswith("prompt"):
        rest = stripped[len("prompt"):].strip()
        if rest.startswith("//"):
            styles[cur]["comment"] = rest[2:].strip()
        continue

# Templates are the 6-space-indented continuation lines after the `prompt` key.
lines = body.split("\n")
cur = None
collect = False
for raw in lines:
    if raw.strip().startswith("//"):
        continue
    if not raw.strip():
        if collect:
            styles[cur]["suffix"] += "\n"
        continue
    indent = len(raw) - len(raw.lstrip())
    stripped = raw.strip()
    if indent == 2 and stripped not in ("prompt", "negativePrompt", "description"):
        cur, collect = stripped, False
        continue
    if indent == 4 and stripped.startswith("prompt"):
        # inline template on the same line?
        rest = stripped[len("prompt"):].strip()
        if rest.startswith("//"):
            rest = ""
        collect = True
        if rest:
            styles[cur]["suffix"] += rest + "\n"
        continue
    if collect and indent >= 6:
        # strip leading indent, keep newlines
        styles[cur]["suffix"] += raw[6:] + "\n"

out = []
for name in order:
    suffix = styles[name]["suffix"].strip()
    if suffix:
        out.append({"name": name, "suffix": re.sub(r"\n{3,}", "\n\n", suffix),
                    "comment": styles[name]["comment"]})

print(f"parsed {len(out)} styles with a prompt template")
for s in out[:3]:
    print(f"\n--- {s['name']} ---\n{s['suffix'][:220]}")

dest = sys.argv[2] if len(sys.argv) > 2 else "/tmp/opencode/styles.json"
json.dump(out, open(dest, "w"), indent=2)
print(f"\nwrote {dest}")
