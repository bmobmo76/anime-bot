import asyncio, base64, glob, json, os, re, shutil, subprocess, urllib.parse
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MAPPER = "https://mapper.nekostream.site/api/mal/"

_cookies = {}
HDRS = {}


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


def http_get(url, ref=None, extra=None, **kw):
    h = _headers(ref)
    if extra:
        h.update(extra)
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
        if ".m3u8" in u or ".mp4" in u or ".mkv" in u:
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

    for m in re.findall(r'https?://[^"\'\\\s]+?\.(?:m3u8|mp4|mkv)[^"\'\\\s]*', html):
        found.append(m.replace("\\/", "/"))
    return list(dict.fromkeys(found))


async def js_html(url, wait_ms=7000):
    b = await _get_browser()
    ctx = await b.new_context(user_agent=UA, locale="en-US")
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(wait_ms)
        return await page.content()
    finally:
        await ctx.close()


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


def _q(k):
    m = re.match(r"(\d+)", k)
    return int(m.group(1)) if m else 0


def _is_media(url):
    try:
        r = creq.head(url, headers=_headers(url), impersonate="chrome",
                      timeout=30, allow_redirects=True)
    except Exception:
        return True
    if r.status_code != 200:
        return False
    ct = r.headers.get("content-type", "").lower()
    return any(x in ct for x in ("video", "octet", "mpeg", "matroska", "mp4"))


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
        return self.parse_eps(http_get(url).text, url)

    def parse_eps(self, html, url):
        url = url.rstrip("/")
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.select("#w-episodes a[href]"):
            m = re.search(r"/ep-([0-9]+(?:\.[0-9]+)?)", a.get("href") or "")
            num = a.get("data-num") or (m.group(1) if m else None)
            if not num:
                continue
            out.append({"num": str(num), "id": a.get("data-id"),
                        "url": f"{url}/ep-{num}",
                        "mal": a.get("data-mal"),
                        "slug": a.get("data-slug"),
                        "ts": a.get("data-timestamp")})
        out.sort(key=lambda x: float(x["num"]))
        return _dedup(out)

    def _table(self, ep, kind):
        mal, slug, ts = ep.get("mal"), ep.get("slug"), ep.get("ts")
        if not (mal and slug and ts):
            return None
        try:
            j = http_get(f"{MAPPER}{mal}/{slug}/{ts}").json()
        except Exception:
            return None
        for src in j.values():
            if not isinstance(src, dict) or not isinstance(src.get(kind), dict):
                continue
            t = {k: v for k, v in (src[kind].get("download") or {}).items() if v}
            if t:
                return t
        return None

    def short_link(self, ep, height, kind):
        t = self._table(ep, kind)
        if not t:
            return None
        ok = [k for k in t if _q(k) <= height]
        return t[max(ok, key=_q) if ok else min(t, key=_q)]

    def mapper_file(self, ep, height, kind):
