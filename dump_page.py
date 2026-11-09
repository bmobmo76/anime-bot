import json
import re
import sys
from curl_cffi import requests as creq

SHOW = sys.argv[1] if len(sys.argv) > 1 else "i323Phb6xziB8TvpH"
EP = sys.argv[2] if len(sys.argv) > 2 else "2"
BASE = f"https://mkissa.to/watch/{SHOW}/p-{EP}-sub"

s = creq.Session(impersonate="chrome")
s.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://mkissa.to/",
})

html = s.get(BASE, timeout=45).text
print("page len:", len(html))

for probe in (BASE + "/__data.json", "https://mkissa.to/api/aa/crypto",
              "https://api.mkissa.net/crypto", "https://mkissa.to/api/crypto"):
    try:
        r = s.get(probe, timeout=30)
        print("PROBE", probe, r.status_code, len(r.text))
        if r.status_code == 200 and "partB" in r.text:
            print("HIT", probe, r.text[:600])
    except Exception as exc:
        print("PROBE", probe, "ERR", type(exc).__name__)

js = set(re.findall(r'import\("(https://[^"]+\.js)"\)', html))
js |= set(re.findall(r'src="(https://[^"]+\.js)"', html))
print("js files:", len(js))

KEY = re.compile(r'(aaCrypto|partB|epoch|crypto)', re.I)
for url in list(js)[:8]:
    try:
        r = s.get(url, timeout=60)
    except Exception as exc:
        print("JS ERR", url, type(exc).__name__)
        continue
    body = r.text
    print("JS", url, r.status_code, len(body))
    if not KEY.search(body):
        continue
    for m in KEY.finditer(body):
        a = max(0, m.start() - 220)
        b = min(len(body), m.end() + 220)
        print("   ...", body[a:b].replace("\n", " "))
        if m.start() > 400000:
            break
