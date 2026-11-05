import asyncio, glob, os, re, subprocess, urllib.parse
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_cookies = {}


def _headers(ref=None):
    h = {"User-Agent": UA,
         "Accept-Language": "en-US,en;q=0.9",
         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    if ref:
        h["Referer"] = ref
    return h


def _cookie_header(url):
    host = urllib.parse.urlsplit(url).netloc
    for base, jar in _cookies.items():
        if urllib.parse.urlsplit(base).netloc == host and jar:
            return "; ".join(f"{c['name']}={c['value']}" for c in jar)
    return None


def http_get(url, ref=None, **kw):
    h = _headers(ref)
    ck = _cookie_header(url)
    if ck:
        h["Cookie"] = ck
    return creq.get(url, headers=h, impersonate="chrome", timeout=45,
                    allow_redirects=True, **kw)


def http_post(url, data, ref=None):
    h = _headers(ref)
    ck = _cookie_header(url)
    if ck:
        h["Cookie"] = ck
    return creq.post(url, headers=h, data=data, impersonate="chrome", timeout=45)


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
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"])
    return _browser


async def warm(base):
    """کوکی و clearance را با یک بازدید واقعی می‌گیرد."""
    try:
        b = await _get_browser()
        ctx = await b.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()
        try:
            await page.goto(base, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4000)
            _cookies[base] = await ctx.cookies()
        finally:
            await ctx.close()
    except Exception:
        _cookies[base] = []


async def warm_all():
    await asyncio.gather(*[warm(s.base) for s in SITES.values()])


async def js_capture(url, ref=None, wait_ms=9000):
    """صفحه را باز می‌کند، دکمه پخش را می‌زند و درخواست‌های شبکه را شنود می‌کند."""
    b = await _get_browser()
    hdr = {"Referer": ref} if ref else {}
    ctx = await b.new_context(user_agent=UA, locale="en-US",
                              extra_http_headers=hdr)
    found = []
    page = await ctx.new_page()

    def on_req(req):
        u = req.url
        if ".m3u8" in u or ".mp4" in u:
            found.append(u)

    page.on("request", on_req)
    html = ""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.evaluate(
                "() => { for (const s of document.querySelectorAll("
                "'video,button,.play,.vjs-big-play-button,.jw-icon-display,"
                "'[class*=play]')) { try { s.click(); } catch(e){} } }")
        except Exception:
            pass
        step = 500
        for _ in range(int(wait_ms / step)):
            if found:
                break
            await page.wait_for_timeout(step)
        html = await page.content()
    finally:
        await ctx.close()

    for m in re.findall(r'https?://[^"\'\\\s]+?\.(?:m3u8|mp4)[^"\'\\\s]*', html):
        found.append(m.replace("\\/", "/"))
    for m in re.findall(r'"file"\s*:\s*"([^"]+)"', html):
        u = m.replace("\\/", "/")
        if u.startswith("//"):
            u = "https:" + u
        if ".m3u8" in u or ".mp4" in u:
            found.append(u)
    return _dedup(found, key=None) if False else list(dict.fromkeys(found))


def _best(urls):
    if not urls:
        return None
    m3u8 = [u for u in urls if ".m3u8" in u]
    for u in m3u8:
        if "master" in u.lower():
            return u
    if m3u8:
        return m3u8[0]
    return urls[0]


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
            num = (a.get("data-number") or a.get("data-num")
                   or a.get_text(strip=True))
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
                if "episode" in href or href.count("/") > 4:
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
            j = r.json()
        except Exception:
            return None
        return j.get("file") or j.get("url")


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
        for attempt in range(2):
            try:
                return await asyncio.to_thread(site.search, query)
            except Exception:
                if attempt == 0:
                    await warm(site.base)
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
        found = await js_capture(url, ref)
    except Exception:
        found = []
    got = _best(found)
    if got:
        return got
    if depth >= 2:
        return None
    try:
        html = await asyncio.to_thread(lambda: http_get(url, ref).text)
    except Exception:
        return None
    for ifr in re.findall(r'<iframe[^>]+src="([^"]+)"', html):
        ifr = ifr.replace("\\/", "/")
        if ifr.startswith("//"):
            ifr = "https:" + ifr
        ifr = urllib.parse.urljoin(url, ifr)
        g = await generic_resolve(ifr, url, depth + 1, seen)
        if g:
            return g
    return None


async def resolve(site_key, ep, kind):
    site = SITES[site_key]
    cand = None
    if isinstance(site, WCO):
        try:
            cand = await asyncio.to_thread(site.resolve, ep["url"])
        except Exception:
            cand = None
    if not cand and ep.get("id"):
        try:
            servers = await asyncio.to_thread(site.servers, ep["id"])
        except Exception:
            servers = []
        wanted = [s for s in servers if s["kind"] == kind] or servers
        for s in wanted[:5]:
            try:
                u = await asyncio.to_thread(site.source, s["id"])
            except Exception:
                u = None
            if u:
                cand = u
                break
    if cand and (".m3u8" in cand or ".mp4" in cand):
        return cand
    if cand:
        got = await generic_resolve(cand, ep["url"])
        if got:
            return got
    return await generic_resolve(ep["url"], ep["url"])


def download(url, ref, kind, height, outdir="/tmp/dl"):
    os.makedirs(outdir, exist_ok=True)
    tmpl = os.path.join(outdir, "ep.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--no-cache-dir",
           "--retries", "5", "--fragment-retries", "20",
           "--concurrent-fragments", "8", "--no-check-certificates",
           "--socket-timeout", "30",
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


LIMIT = 42 * 1024 * 1024


def split(path, limit=LIMIT):
    size = os.path.getsize(path)
    if size <= limit:
        parts = [path]
    else:
        dur = 0.0
        try:
            p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "format=duration", "-of",
                                "default=nw=1:nk=1", path],
                               capture_output=True, text=True)
            dur = float(p.stdout.strip())
        except Exception:
            dur = 0.0
        seg = 300 if dur <= 0 else max(45, int(dur * (limit / size) * 0.8))
        base = os.path.join(os.path.dirname(path), "part%03d.mp4")
        subprocess.run(["ffmpeg", "-y", "-i", path, "-c", "copy",
                        "-map", "0:v", "-map", "0:a?", "-f", "segment",
                        "-segment_time", str(seg), "-reset_timestamps", "1",
                        base], check=True, capture_output=True)
        parts = sorted(glob.glob(os.path.join(os.path.dirname(path), "part*.mp4")))
        parts = parts or [path]

    # faststart تا تلگرام ویدیو را قبول کند
    out = []
    for p in parts:
        fs = p + ".fs.mp4"
        r = subprocess.run(["ffmpeg", "-y", "-i", p, "-c", "copy",
                            "-movflags", "+faststart", fs],
                           capture_output=True)
        if r.returncode == 0 and os.path.getsize(fs) > 0:
            os.remove(p)
            out.append(fs)
        else:
            out.append(p)
    return out


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


async def health():
    lines = []
    for key, site in SITES.items():
        def probe(base=site.base):
            try:
                return http_get(base, timeout=25).status_code
            except Exception as e:
                return f"خطا {type(e).__name__}"
        code = await asyncio.to_thread(probe)
        if code in (403, 503):
            await warm(site.base)
            code = await asyncio.to_thread(probe)
        mark = "✅" if code == 200 else "❌"
        lines.append(f"{mark} {site.name}: {code}")
    return "\n".join(lines)
