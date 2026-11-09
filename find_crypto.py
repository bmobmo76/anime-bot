import re
import sys
from curl_cffi import requests as creq

APP = "https://cdn.mkissa.net/all/mk/_app/immutable/entry/app.BtBRNhAS.js"
CDN = "https://cdn.mkissa.net"

s = creq.Session(impersonate="chrome")
s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/131.0.0.0 Safari/537.36"})

print("=== api/aa/crypto with json accept ===")
try:
    r = s.get("https://mkissa.to/api/aa/crypto", timeout=20,
              headers={"Accept": "application/json"})
    print(r.status_code, r.headers.get("content-type"), r.text[:200])
except Exception as exc:
    print("ERR", type(exc).__name__)

app = s.get(APP, timeout=30).text
print("app.js len:", len(app))

refs = set(re.findall(r'"(\./[\w./-]+\.js)"', app))
refs |= set(re.findall(r'"(/[\w./-]*_app/[\w./-]+\.js)"', app))
refs |= set(re.findall(r'(?:import|from)\("?([\w./-]+\.js)"?\)', app))
print("refs:", len(refs))

cands = []
for ref in sorted(refs):
    if ref.startswith("./"):
        url = CDN + "/_app/immutable/entry/" + ref[2:]
    elif ref.startswith("/"):
        url = CDN + ref
    else:
        url = CDN + "/_app/immutable/entry/" + ref
    cands.append(url)

# also brute the node chunk for route node id 20
for name in ("20", "20."):
    pass
m = re.search(r'nodes/20[\.\-][\w-]+\.js', app)
if m:
    print("node20 chunk:", m.group(0))

KEY = re.compile(r'(aaCrypto|partB|__aa|epoch)', re.I)
tried = 0
for url in cands:
    if tried >= 40:
        break
    tried += 1
    try:
        r = s.get(url, timeout=30)
    except Exception:
        continue
    body = r.text
    if not KEY.search(body):
        continue
    print("HIT", url, r.status_code, len(body))
    for mm in list(KEY.finditer(body))[:6]:
        a = max(0, mm.start() - 300)
        b = min(len(body), mm.end() + 400)
        print("  >>>", body[a:b].replace("\n", " "))
