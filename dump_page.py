import json
import re
import subprocess
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

print("=== __data.json ===")
r = s.get(BASE + "/__data.json", timeout=30)
print(r.status_code, r.text[:300])

print("=== yt-dlp version ===")
print(subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip())

print("=== relevant extractors ===")
out = subprocess.run(["yt-dlp", "--list-extractors"], capture_output=True, text=True).stdout
pat = re.compile(r"(anime|anizone|allanime|allmanga|kissa|kick|gg|wave|pahe|hianime|gogo|9anime|senshi|miruro|anify|onsen|zoro|ani)", re.I)
for line in out.splitlines():
    if pat.search(line):
        print(line)

print("=== yt-dlp on mkissa page ===")
res = subprocess.run(
    ["yt-dlp", "-J", "--no-warnings", "--skip-download", BASE],
    capture_output=True, text=True,
)
print("rc:", res.returncode)
print((res.stdout or "")[:600])
print("stderr:", (res.stderr or "")[:600])
