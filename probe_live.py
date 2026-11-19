import json
import re
import shutil
import subprocess
import traceback

import keygen_live as kl

print("node:", shutil.which("node"))
print("npm:", shutil.which("npm"))
print("git:", shutil.which("git"))

for cmd in ("node", "npm"):
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=30)
        print(cmd, "->", out.stdout.strip(), out.stderr.strip()[:100])
    except Exception as exc:
        print(cmd, "ERR", type(exc).__name__, exc)

s = kl._session()
html = s.get(kl.MKISSA, timeout=20).text
print("html len:", len(html))
m = re.search(r"/entry/app\.[A-Za-z0-9_.-]+\.js", html)
print("app path:", m.group() if m else None)
if m:
    app_js = s.get(kl.CDN + m.group(), timeout=20).text
    print("app len:", len(app_js))
    chunks = re.findall(r'"../(chunks/[A-Za-z0-9_.-]+\.js)"', app_js)[:5]
    print("chunks:", chunks)
    for ch in chunks:
        cj = s.get(f"{kl.CDN}/{ch}", timeout=20).text
        hit = ("VaildTranslationTypeEnumType" in cj) or ("x-aa-boot" in cj)
        b = re.search(r'!=="string"\?"([0-9]+)"', cj)
        l = re.search(r'const ..="(k[0-9]+)"', cj)
        print("chunk", ch, "len", len(cj), "marker", hit, "build", b.group(1) if b else None,
              "lane", l.group(1) if l else None)
        if hit and b and l:
            try:
                emb = kl._mask_from_chunk(cj)
                print("embedded len:", len(emb))
            except Exception:
                traceback.print_exc(limit=3)
            break

print("=== full fetch_live ===")
try:
    print(json.dumps(kl.fetch_live(), indent=2))
except Exception:
    traceback.print_exc()
