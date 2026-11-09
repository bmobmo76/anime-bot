import re
import sys
from curl_cffi import requests as creq

SHOW = sys.argv[1] if len(sys.argv) > 1 else "i323Phb6xziB8TvpH"
EP = sys.argv[2] if len(sys.argv) > 2 else "2"

url = f"https://mkissa.to/watch/{SHOW}/p-{EP}-sub"
s = creq.Session(impersonate="chrome")
s.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://youtu-chan.com/",
})
r = s.get(url, timeout=45)
print("status:", r.status_code, "len:", len(r.text))
print("---- JS/CSS refs ----")
for m in re.findall(r'(?:src|href)="([^"]+\.(?:js|mjs))"', r.text):
    print(m)
print("---- BODY START ----")
print(r.text)
print("---- BODY END ----")
