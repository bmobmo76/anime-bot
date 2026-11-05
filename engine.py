import asyncio, base64, glob, json, os, re, shutil, subprocess, urllib.parse
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_cookies = {}


def _headers(ref=None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
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
    kw.setdefault("timeout", 45)
    kw.setdefault("allow_redirects", True)
    kw.setdefault("impersonate", "chrome")
    return creq.get(url, headers=h, **kw)


def http_post(url, data, ref=None, **kw):
    h = _headers(ref)
    ck = _cookie_header(url)
    if ck:
        h["Cookie"] = ck
    kw.setdefault("timeout", 45)
    kw.setdefault("impersonate", "chrome")
    return creq.post(url, headers=h, data=data, **kw)


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


def free_gb(path="/tmp"):
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except Exception:
        return 99.0


_browser = None


async def _get_browser():
    global _browser
    if _browser is None:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", "--mute-audio"])
    return _browser


async def warm(base):
    try:
        b = await _get_browser()
        ctx = await b.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()
        try:
            await page.goto(base, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(6000)
            _cookies[base] = await ctx.cookies()
        finally:
            await ctx.close()
    except Exception:
        _cookies[base] = []


async def warm_all():
    await asyncio.gather(*[warm(s.base) for s in SITES.values()])


async def js_capture(url, ref=None, click=None, wait_ms=12000):
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
        await page.wait_for_timeout(2500)
        if click:
            try:
                await page.click(click, timeout=6000)
            except Exception:
                pass
        try:
            await page.evaluate(
                "() => { for (const s of document.querySelectorAll("
                "'#player,.play,.vjs-big-play-button,.jw-icon-display,"
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
    return list(dict.fromkeys(found))


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


class Kai(Site):
    """خانوادهٔ animekai/anikoto/aniwave"""

    def search(self, q):
        r = http_get(f"{self.base}/filter?keyword={urllib.parse.quote(q)}")
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for item in soup.select("div.item"):
            a = item.select_one("a.name[href*='/watch/']")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if len(title) < 2:
                continue
            url = urllib.parse.urljoin(self.base, a["href"]).split("/ep-")[0]
            out.append({"title": title, "url": url.rstrip("/"), "site": self.key})
        return _dedup(out)[:12]

    def episodes(self, url):
        url = url.rstrip("/")
        html = http_get(url).text
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.select("#w-episodes a[data-num]"):
            num = a.get("data-num")
            if num is None:
                continue
            out.append({"num": str(num), "id": a.get("data-id"),
                        "url": f"{url}/ep-{num}"})
        out.sort(key=lambda x: int(float(x["num"])))
        return _dedup(out)

    def kinds(self, ep_url):
        """کدام نسخه‌ها موجود است."""
        try:
            html = http_get(ep_url).text
        except Exception:
            return set()
        soup = BeautifulSoup(html, "lxml")
        out = set()
        for d in soup.select("#w-servers .type[data-type]"):
            if d.select("li[data-link-id]"):
                out.add(d.get("data-type"))
        return out

    def direct(self, ep_url):
        html = http_get(ep_url).text
        m = re.search(r'download-data="([^"]+)"', html)
        if not m:
            return None
        try:
            raw = base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4))
            for it in json.loads(raw.decode("utf-8", "ignore")):
                if it.get("url"):
                    return it["url"]
        except Exception:
            return None
        return None


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

    def kinds(self, ep_url):
        return {"sub", "dub"}

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


SITES = {}


def _reg(cls):
    s = cls()
    SITES[s.key] = s


class AniKoto(Kai):
    key, name, base = "anikoto", "AniKoto", "https://anikoto.cz"


class AniWaves(Kai):
    key, name, base = "aniwaves", "AniWaves", "https://aniwaves.ru"


class AniWaveBy(Kai):
    key, name, base = "aniwaveby", "AniWave", "https://aniwave.by"


class WcoDub(WCO):
    key, name, base = "wcodub", "WCO (Dub)", "https://wcoanimedub.tv"


class WcoSub(WCO):
    key, name, base = "wcosub", "WCO (Sub)", "https://wcoanimesub.tv"


for c in (AniKoto, AniWaves, AniWaveBy, WcoDub, WcoSub):
    _reg(c)


def norm(t):
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


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
    return flat[:30]


async def generic_resolve(url, ref=None, click=None):
    try:
        found = await js_capture(url, ref, click=click)
    except Exception:
        return None
    return _best(found)


async def resolve_one(site_key, ep, kind):
    site = SITES[site_key]
    if isinstance(site, Kai):
        try:
            u = await asyncio.to_thread(site.direct, ep["url"])
        except Exception:
            u = None
        if u:
            return u
        sel = ("#w-servers .type[data-type='dub'] li" if kind == "dub"
               else "#w-servers .type[data-type='sub'] li")
        got = await generic_resolve(ep["url"], ep["url"], click=sel)
        return got or await generic_resolve(ep["url"], ep["url"])
    try:
        u = await asyncio.to_thread(site.resolve, ep["url"])
    except Exception:
        u = None
    if u and (".m3u8" in u or ".mp4" in u):
        return u
    if u:
        got = await generic_resolve(u, ep["url"])
        if got:
            return got
    return await generic_resolve(ep["url"], ep["url"])


async def resolve(job, ep, kind):
    """اول سایت انتخابی، بعد بقیهٔ سایت‌ها با همان عنوان."""
    got = await resolve_one(job["site"], ep, kind)
    if got:
        return got
    key = norm(job["title"])
    for sk, site in SITES.items():
        if sk == job["site"]:
            continue
        try:
            res = await asyncio.to_thread(site.search, job["title"])
        except Exception:
            continue
        hit = next((r for r in res if norm(r["title"])[:22] == key[:22]), None)
        if not hit:
            continue
        try:
            eps = await asyncio.to_thread(site.episodes, hit["url"])
        except Exception:
            continue
        e2 = next((e for e in eps if e["num"] == ep["num"]), None)
        if not e2:
            continue
        got = await resolve_one(sk, e2, kind)
        if got:
            return got
    return None


def download(url, ref, kind, height, outdir, on_progress=None):
    os.makedirs(outdir, exist_ok=True)
    tmpl = os.path.join(outdir, "ep.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--no-cache-dir",
           "--newline", "--retries", "5", "--fragment-retries", "20",
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

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    for line in p.stdout:
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m and on_progress:
            try:
                on_progress(float(m.group(1)))
            except Exception:
                pass
    rc = p.wait()

    if rc != 0:
        out = os.path.join(outdir, "ep.mp4")
        r2 = subprocess.run(["ffmpeg", "-y", "-headers",
                             f"Referer: {ref}\r\n", "-i", url,
                             "-c", "copy", out], capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError("".join(tail)[-400:])
        return out, None

    files = [f for f in glob.glob(os.path.join(outdir, "ep.*"))
             if not f.endswith(".srt") and not f.endswith(".vtt")]
    if not files:
        raise RuntimeError("فایلی تولید نشد")
    subs = glob.glob(os.path.join(outdir, "ep*.srt"))
    return files[0], (subs[0] if subs else None)


def mux_subs(video, subs):
    if not subs:
        return video
    out = os.path.join(os.path.dirname(video), "muxed.mp4")
    r = subprocess.run(["ffmpeg", "-y", "-i", video, "-i", subs,
                        "-c", "copy", "-c:s", "mov_text",
                        "-metadata:s:s:0", "language=eng",
                        "-movflags", "+faststart", out],
                       capture_output=True)
    if r.returncode == 0 and os.path.getsize(out) > 0:
        return out
    return video


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

    out = []
    for p in parts:
        fs = p + ".fs.mp4"
        r = subprocess.run(["ffmpeg", "-y", "-i", p, "-c", "copy",
                            "-movflags", "+faststart", fs], capture_output=True)
        if r.returncode == 0 and os.path.getsize(fs) > 0:
            os.remove(p)
            out.append(fs)
        else:
            out.append(p)
    return out


def thumb(video, outdir):
    p = os.path.join(outdir, "thumb.jpg")
    r = subprocess.run(["ffmpeg", "-y", "-ss", "60", "-i", video,
                        "-frames:v", "1", "-vf", "scale=320:-1", p],
                       capture_output=True)
    return p if r.returncode == 0 and os.path.exists(p) else None


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
                return f"{type(e).__name__}"
        code = await asyncio.to_thread(probe)
        if code in (403, 503):
            await warm(site.base)
            code = await asyncio.to_thread(probe)
        lines.append(f"{'✅' if code == 200 else '❌'} {site.name}: {code}")
    return "\n".join(lines)
