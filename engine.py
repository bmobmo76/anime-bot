import asyncio, glob, os, re, subprocess, urllib.parse
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

def _headers(ref=None):
    h = {"User-Agent": UA,
         "Accept-Language": "en-US,en;q=0.9",
         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    if ref:
        h["Referer"] = ref
    return h

def http_get(url, ref=None, **kw):
    return creq.get(url, headers=_headers(ref), impersonate="chrome",
                    timeout=45, allow_redirects=True, **kw)

def http_post(url, data, ref=None):
    return creq.post(url, headers=_headers(ref), data=data,
                     impersonate="chrome", timeout=45)

def origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"

def _dedup(items, key="url"):
    seen, out = set(), []
    for i in items:
        if i[key] in seen:
            continue
        seen.add(i[key])
        out.append(i)
    return out

_browser = None

async def _get_browser():
    global _browser
    if _browser is None:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    return _browser

async def js_get(url, ref=None, wait=4500):
    b = await _get_browser()
    hdr = {"Referer": ref} if ref else {}
    ctx = await b.new_context(user_agent=UA, locale="en-US", extra_http_headers=hdr)
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(wait)
        return await page.content()
    finally:
        await ctx.close()


class Site:
    key = ""
    name = ""
    base = ""

    def search(self, q):
        raise NotImplementedError


class KaiStyle(Site):
    def search(self, q):
        r = http_get(f"{self.base}/ajax/anime/search?keyword={urllib.parse.quote(q)}")
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for a in soup.select("a[href*='/watch/']"):
            title = a.get_text(" ", strip=True)
            if not title:
                title = a["href"].rstrip("/").split("/")[-1].replace("-", " ").title()
            out.append({"title": title,
                        "url": urllib.parse.urljoin(self.base, a["href"]),
                        "site": self.key})
        return _dedup(out)

    def _anime_id(self, url):
        try:
            html = http_get(url).text
        except Exception:
            return None
        m = re.search(r'data-id="(\d+)"', html)
        if m:
            return m.group(1)
        m = re.search(r"-(\d+)/?$", url)
        return m.group(1) if m else None

    def episodes(self, url):
        aid = self._anime_id(url)
        if not aid:
            return []
        r = http_get(f"{self.base}/ajax/episode/list/{aid}", ref=url)
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for a in soup.select("a[data-id]"):
            num = a.get("data-num") or a.get_text(strip=True)
            out.append({"num": str(num), "id": a["data-id"], "url": url})
        return out

    def servers(self, ep_id):
        r = http_get(f"{self.base}/ajax/episode/servers?episodeId={ep_id}")
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for a in soup.select("[data-id]"):
            label = " ".join(filter(None, [
                a.get("data-tip"), a.get("data-server-type"),
                a.get("data-type"), a.get_text(" ", strip=True)])).lower()
            kind = "dub" if "dub" in label else ("sub" if "sub" in label else "")
            out.append({"id": a["data-id"], "kind": kind})
        return out

    def source(self, server_id):
        r = http_get(f"{self.base}/ajax/episode/sources?id={server_id}")
        try:
            j = r.json()
        except Exception:
            return None
        return j.get("url") or (j.get("result") or {}).get("url")


class WCO(Site):
    def search(self, q):
        for path in ("/search?query=", "/?s="):
            try:
                r = http_get(self.base + path + urllib.parse.quote(q))
            except Exception:
                continue
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            out = []
            for a in soup.select("a[href]"):
                href = a["href"]
                if "episode" in href or href.count("/") > 3:
                    continue
                title = a.get("title") or a.get_text(" ", strip=True)
                if len(title) < 3:
                    continue
                out.append({"title": title,
                            "url": urllib.parse.urljoin(self.base, href),
                            "site": self.key})
            out = _dedup(out)
            if out:
                return out[:12]
        return []

    def episodes(self, url):
        html = http_get(url).text
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.select("a[href]"):
            m = re.search(r"episode[-/]?(\d+)", a["href"])
            if m:
                out.append({"num": m.group(1),
                            "url": urllib.parse.urljoin(self.base, a["href"])})
        out.sort(key=lambda x: int(x["num"]))
        return _dedup(out)

    def resolve(self, ep_url):
        html = http_get(ep_url).text
        m = re.search(r'data-apireq="([^"]+)"', html)
        if not m:
            return None
        r = http_post(self.base + "/inc/embed/getvidlink.php",
                      {"data": m.group(1)}, ref=ep_url)
        try:
            return r.json().get("file")
        except Exception:
            return None


class GenericSite(Site):
    def search(self, q):
        qq = urllib.parse.quote(q)
        for path in (f"/search?q={qq}", f"/?s={qq}", f"/search/{qq}"):
            try:
                r = http_get(self.base + path)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            out = []
            for a in soup.select("a[href]"):
                href = a["href"]
                if not any(x in href for x in ("/watch/", "/anime/", "/series/")):
                    continue
                title = a.get_text(" ", strip=True)
                if len(title) < 3:
                    continue
                out.append({"title": title,
                            "url": urllib.parse.urljoin(self.base, href),
                            "site": self.key})
            out = _dedup(out)
            if out:
                return out[:12]
        return []

    def episodes(self, url):
        html = http_get(url).text
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.select("a[href]"):
            m = re.search(r"episode[-/]?(\d+)", a["href"])
            if m:
                out.append({"num": m.group(1),
                            "url": urllib.parse.urljoin(self.base, a["href"])})
        out.sort(key=lambda x: int(x["num"]))
        return _dedup(out)


SITES = {}

def _register(cls):
    s = cls()
    SITES[s.key] = s

class AniKoto(KaiStyle):
    key, name, base = "anikoto", "AniKoto", "https://anikoto.tv"
class AniWave(KaiStyle):
    key, name, base = "aniwave", "AniWave", "https://aniwave.ru"
class WcoDub(WCO):
    key, name, base = "wcodub", "WCO (Dub)", "https://wcoanimedub.tv"
class WcoSub(WCO):
    key, name, base = "wcosub", "WCO (Sub)", "https://wcoanimesub.tv"
class Isekai(GenericSite):
    key, name, base = "isekai", "Isekai2nd", "https://isekai2nd.com"

for c in (AniKoto, AniWave, WcoDub, WcoSub, Isekai):
    _register(c)


async def search_all(query):
    async def one(site):
        try:
            return await asyncio.to_thread(site.search, query)
        except Exception:
            return []
    chunks = await asyncio.gather(*[one(s) for s in SITES.values()])
    flat = [x for c in chunks for x in c]
    flat.sort(key=lambda x: len(x["title"]))
    return flat[:20]


async def generic_resolve(url, ref=None, depth=0, seen=None):
    seen = seen or set()
    if url in seen or depth > 2:
        return None
    seen.add(url)
    try:
        html = await js_get(url, ref)
    except Exception:
        try:
            html = await asyncio.to_thread(lambda: http_get(url, ref).text)
        except Exception:
            return None

    m = re.search(r'https?://[^"\'\\\s]+?\.m3u8[^"\'\\\s]*', html)
    if m:
        return m.group(0).replace("\\/", "/")
    m = re.search(r'https?://[^"\'\\\s]+?\.mp4[^"\'\\\s]*', html)
    if m:
        return m.group(0).replace("\\/", "/")

    for ifr in re.findall(r'<iframe[^>]+src="([^"]+)"', html):
        ifr = ifr.replace("\\/", "/")
        if ifr.startswith("//"):
            ifr = "https:" + ifr
        ifr = urllib.parse.urljoin(url, ifr)
        got = await generic_resolve(ifr, url, depth + 1, seen)
        if got:
            return got
    return None


async def resolve(site_key, ep, kind):
    site = SITES[site_key]
    if isinstance(site, WCO):
        u = await asyncio.to_thread(site.resolve, ep["url"])
        if u:
            return u
    if ep.get("id"):
        servers = await asyncio.to_thread(site.servers, ep["id"])
        wanted = [s for s in servers if s["kind"] == kind] or servers
        for s in wanted[:5]:
            u = await asyncio.to_thread(site.source, s["id"])
            if u:
                return u
    return await generic_resolve(ep["url"], ep["url"])


def download(url, ref, kind, height, outdir="/tmp/dl"):
    os.makedirs(outdir, exist_ok=True)
    tmpl = os.path.join(outdir, "ep.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--retries", "5",
           "--fragment-retries", "20", "--concurrent-fragments", "8",
           "--no-check-certificates",
           "-f", f"bv*[height<={height}]+ba/b[height<={height}]/b",
           "--merge-output-format", "mp4", "--remux-video", "mp4",
           "-o", tmpl]
    if ref:
        cmd += ["--referer", ref, "--add-header", f"Origin:{origin(ref)}"]
    if kind == "sub":
        cmd += ["--write-subs", "--write-auto-subs",
                "--sub-langs", "en,en-US,eng", "--convert-subs", "srt"]
    cmd.append(url)

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        # تلاش دوم با ffmpeg خام
        out = os.path.join(outdir, "ep.mp4")
        ff = ["ffmpeg", "-y", "-headers", f"Referer: {ref}\r\n",
              "-i", url, "-c", "copy", out]
        r2 = subprocess.run(ff, capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError((res.stderr or "")[-400:])
        return out, None

    files = [f for f in glob.glob(os.path.join(outdir, "ep.*"))
             if not f.endswith(".srt") and not f.endswith(".vtt")]
    if not files:
        raise RuntimeError("فایلی تولید نشد")
    video = files[0]
    subs = glob.glob(os.path.join(outdir, "ep*.srt"))
    return video, (subs[0] if subs else None)


def split(path, limit=48 * 1024 * 1024):
    size = os.path.getsize(path)
    if size <= limit:
        return [path]
    dur = 0.0
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of",
                            "default=nw=1:nk=1", path],
                           capture_output=True, text=True)
        dur = float(p.stdout.strip())
    except Exception:
        dur = 0.0
    if dur <= 0:
        seg = 300
    else:
        seg = max(60, int(dur * (limit / size) * 0.85))
    base = os.path.join(os.path.dirname(path), "part%03d.mp4")
    subprocess.run(["ffmpeg", "-y", "-i", path, "-c", "copy", "-map", "0",
                    "-f", "segment", "-segment_time", str(seg),
                    "-reset_timestamps", "1", base],
                   check=True, capture_output=True)
    parts = sorted(glob.glob(os.path.join(os.path.dirname(path), "part*.mp4")))
    return parts or [path]


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
