import asyncio, base64, glob, json, os, re, shutil, subprocess, urllib.parse
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MAPPER = "https://mapper.nekostream.site/api/mal/"

_cookies = {}
HDRS = {}
WLIST = {}


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


async def _call(fn, *a):
    r = fn(*a)
    if asyncio.iscoroutine(r):
        return await r
    return r


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


async def js_html(url, wait_ms=6000, ref=None):
    b = await _get_browser()
    hdr = {"Referer": ref} if ref else {}
    ctx = await b.new_context(user_agent=UA, locale="en-US",
                              extra_http_headers=hdr)
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(wait_ms)
        return await page.content()
    finally:
        await ctx.close()


async def _fetch(url, ref=None):
    try:
        r = await asyncio.to_thread(lambda: http_get(url, ref))
        if r.status_code == 200 and len(r.text) > 2000:
            return r.text
    except Exception:
        pass
    try:
        return await js_html(url, 5000, ref)
    except Exception:
        return ""


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
    """خانوادهٔ animekai/anikoto/aniwave + kwik"""

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
        pick = self.short_link(ep, height, kind)
        if not pick:
            return None
        m = re.search(r"/([A-Za-z0-9]+)/?$", pick)
        if not m:
            return None
        fid = m.group(1)
        nav = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
               "Sec-Fetch-Site": "cross-site",
               "Upgrade-Insecure-Requests": "1"}
        tries = [
            (f"https://testpahe.nekostream.site/{fid}", pick, nav),
            (f"https://testpahe.nekostream.site/{fid}", pick, None),
            (f"https://testpahe.nekostream.site/{fid}",
             "https://pahe.nekostream.site/", None),
            (f"https://pahe.nekostream.site/{fid}", pick, None),
        ]
        for url, ref, extra in tries:
            try:
                rr = http_get(url, ref=ref, extra=extra)
            except Exception:
                continue
            ct = rr.headers.get("content-type", "").lower()
            if rr.status_code == 200 and any(
                    x in ct for x in ("video", "octet", "mpeg", "matroska", "mp4")):
                return rr.url
        return None

    async def browser_file(self, short_url):
        b = await _get_browser()
        ctx = await b.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()
        found = []

        def on_req(req):
            if ".mp4" in req.url:
                found.append(req.url)

        page.on("request", on_req)
        try:
            await page.goto(short_url, referer="https://pahe.nekostream.site/",
                            wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)

            for _ in range(3):
                if "pahe.nekostream.site" not in page.url:
                    break
                for sel in ("button.btn", "button", "a.btn"):
                    try:
                        await page.click(sel, timeout=3000)
                        break
                    except Exception:
                        continue
                await page.wait_for_timeout(2500)

            if "kwik.cx" in page.url:
                try:
                    u = await self.kwik_mp4(page)
                except Exception:
                    u = None
                if u:
                    found.append(u)
                if not found:
                    for sel in ("#btn-download", "button.btn", "a.btn", "button"):
                        try:
                            await page.click(sel, timeout=3000)
                            break
                        except Exception:
                            continue
                    for _ in range(36):
                        if found:
                            break
                        await page.wait_for_timeout(500)
                if not found:
                    try:
                        html = await page.content()
                        for m in re.findall(
                                r'https?://[^"\'\\\s]+?\.mp4[^"\'\\\s]*', html):
                            found.append(m.replace("\\/", "/"))
                    except Exception:
                        pass

            final = found[0] if found else page.url
            ck = await ctx.cookies()
            cookie = "; ".join(f"{c['name']}={c['value']}" for c in ck)
            return final, cookie
        finally:
            await ctx.close()

    async def kwik_mp4(self, page):
        info = await page.evaluate("""() => {
            const f = document.querySelector('form');
            if (!f) return null;
            const d = {};
            new FormData(f).forEach((v, k) => { d[k] = v; });
            return {action: f.getAttribute('action') || location.pathname,
                    data: d};
        }""")
        if not info:
            return None
        body = "&".join(f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}"
                        for k, v in info["data"].items())
        txt = await page.evaluate(
            """async ([action, body]) => {
                const r = await fetch(action, {method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: body});
                return await r.text();
            }""", [info["action"], body])
        if not txt:
            return None

        cand = re.findall(r'https?://[^"\'\\\s|]+?\.mp4[^"\'\\\s|]*', txt)
        if cand:
            return cand[0].replace("\\/", "/")

        for b64 in re.findall(r'[A-Za-z0-9+/]{60,}={0,2}', txt):
            try:
                dec = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode(
                    "utf-8", "ignore")
            except Exception:
                continue
            m = re.search(r'https?://[^"\'\\\s]+?\.mp4[^"\'\\\s]*', dec)
            if m:
                return m.group(0).replace("\\/", "/")
        return None

    def ajax_source(self, ep_url, kind):
        try:
            html = http_get(ep_url).text
        except Exception:
            return None
        soup = BeautifulSoup(html, "lxml")
        box = (soup.select_one(f"#w-servers .type[data-type='{kind}']")
               or soup.select_one("#w-servers .type"))
        if not box:
            return None
        for li in box.select("li[data-link-id]")[:4]:
            lid = li.get("data-link-id")
            sv = li.get("data-sv-id")
            if not lid:
                continue
            h = _headers(ep_url)
            h["X-Requested-With"] = "XMLHttpRequest"
            try:
                r = creq.get(f"{self.base}/ajax/server",
                             params={"get": sv, "id": lid}, headers=h,
                             impersonate="chrome", timeout=30)
                res = r.json().get("result")
                if isinstance(res, dict) and res.get("url"):
                    return res["url"]
            except Exception:
                continue
        return None


class WCO(Site):
    async def _list(self):
        if self.key in WLIST:
            return WLIST[self.key]
        url = self.base + ("/dubbed-anime-list" if "dub" in self.key
                           else "/subbed-anime-list")
        html = await _fetch(url)
        soup = BeautifulSoup(html or "", "lxml")
        out = []
        for a in soup.select("a[href*='/anime/']"):
            t = a.get_text(" ", strip=True)
            if len(t) < 2:
                continue
            out.append({"title": t,
                        "url": urllib.parse.urljoin(self.base, a["href"]),
                        "site": self.key})
        WLIST[self.key] = _dedup(out)
        return WLIST[self.key]

    async def search(self, q):
        n = q.lower().strip()
        if not n:
            return []
        items = await self._list()
        if not items:
            html = await _fetch(
                f"{self.base}/search?query={urllib.parse.quote(q)}")
            soup = BeautifulSoup(html or "", "lxml")
            for a in soup.select("a[href*='/anime/']"):
                t = a.get_text(" ", strip=True)
                if len(t) > 2:
                    items.append({"title": t,
                                  "url": urllib.parse.urljoin(self.base, a["href"]),
                                  "site": self.key})
        hit = [i for i in items if n in i["title"].lower()]
        hit.sort(key=lambda x: len(x["title"]))
        return hit[:10]

    async def episodes(self, url):
        html = await _fetch(url)
        soup = BeautifulSoup(html or "", "lxml")
        out = []
        for a in soup.select("a[href]"):
            m = re.search(r"episode[-/]?(\d+)", a["href"])
            if m:
                out.append({"num": m.group(1),
                            "url": urllib.parse.urljoin(self.base, a["href"])})
        out.sort(key=lambda x: int(x["num"]))
        return _dedup(out)

    async def resolve(self, ep_url):
        html = await _fetch(ep_url)
        m = re.search(r'data-apireq="([^"]+)"', html or "")
        if not m:
            return None
        data = m.group(1)
        b = await _get_browser()
        ctx = await b.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()
        try:
            await page.goto(ep_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)
            res = await page.evaluate(
                """async (d) => {
                    const r = await fetch('/inc/embed/getvidlink.php',
                        {method:'POST',
                         headers:{'Content-Type':'application/x-www-form-urlencoded',
                                  'X-Requested-With':'XMLHttpRequest'},
                         body:'data=' + encodeURIComponent(d)});
                    return await r.text();
                }""", data)
            j = json.loads(res)
            return j.get("file") or j.get("url")
        except Exception:
            return None
        finally:
            await ctx.close()


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
    key, name, base = "wcodub", "WCO (Dub)", "https://www.wcoanimedub.tv"


class WcoSub(WCO):
    key, name, base = "wcosub", "WCO (Sub)", "https://www.wcoanimesub.tv"


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
                return await _call(site.search, query) or []
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


async def get_episodes(site_key, url):
    site = SITES[site_key]
    if not isinstance(site, Kai):
        try:
            return await _call(site.episodes, url), ""
        except Exception as e:
            return [], f"خطا: {e}"

    html = ""
    try:
        html = await asyncio.to_thread(lambda: http_get(url).text)
        eps = site.parse_eps(html, url)
        if eps:
            return eps, ""
    except Exception as e:
        html = f"GET خطا: {e}"

    try:
        html2 = await js_html(url)
        eps = site.parse_eps(html2, url)
        if eps:
            return eps, ""
    except Exception:
        pass

    t = re.search(r"<title>(.*?)</title>", html or "", re.S)
    return [], (f"صفحه {len(html or '')} کاراکتر، عنوان: "
                f"{(t.group(1).strip()[:80] if t else 'ندارد')}")


async def resolve_one(site_key, ep, kind, height=720):
    site = SITES[site_key]

    if isinstance(site, Kai):
        mf = (getattr(site, "mapper_file", None)
              or getattr(site, "mapper_file_sync", None))
        u = None
        if mf:
            try:
                u = await asyncio.to_thread(mf, ep, height, kind)
            except Exception:
                u = None
        if u:
            return u

        short = await asyncio.to_thread(site.short_link, ep, height, kind)
        if short:
            try:
                final, cookie = await site.browser_file(short)
            except Exception:
                final, cookie = None, ""
            if final and final != short:
                ok = True
                if not re.search(r"\.(mp4|mkv|m3u8)(\?|$)", final):
                    ok = await asyncio.to_thread(_is_media, final)
                if ok:
                    HDRS[final] = {"Cookie": cookie,
                                   "Referer": "https://kwik.cx/",
                                   "Origin": "https://kwik.cx"}
                    return final

        u = await asyncio.to_thread(site.ajax_source, ep["url"], kind)
        if u and any(x in u for x in (".m3u8", ".mp4", ".mkv")):
            return u
        if u:
            got = await generic_resolve(u, ep["url"])
            if got:
                return got

        sel = ("#w-servers .type[data-type='dub'] li" if kind == "dub"
               else "#w-servers .type[data-type='sub'] li")
        got = await generic_resolve(ep["url"], ep["url"], click=sel)
        return got or await generic_resolve(ep["url"], ep["url"])

    try:
        u = await _call(site.resolve, ep["url"])
    except Exception:
        u = None
    if u and any(x in u for x in (".m3u8", ".mp4", ".mkv")):
        return u
    if u:
        got = await generic_resolve(u, ep["url"])
        if got:
            return got
    return await generic_resolve(ep["url"], ep["url"])


async def resolve(job, ep, kind, height=720):
    got = await resolve_one(job["site"], ep, kind, height)
    if got:
        return got
    key = norm(job["title"])
    for sk, site in SITES.items():
        if sk == job["site"]:
            continue
        try:
            res = await _call(site.search, job["title"]) or []
        except Exception:
            continue
        hit = next((r for r in res if norm(r["title"])[:22] == key[:22]), None)
        if not hit:
            continue
        try:
            eps = await _call(site.episodes, hit["url"])
        except Exception:
            continue
        e2 = next((e for e in eps if e["num"] == ep["num"]), None)
        if not e2:
            continue
        got = await resolve_one(sk, e2, kind, height)
        if got:
            return got
    return None


async def debug_ep(ep, kind, height):
    L = [f"mal={ep.get('mal')} slug={ep.get('slug')} ts={ep.get('ts')}"]
    try:
        t = await asyncio.to_thread(SITES["anikoto"]._table, ep, kind)
        L.append(f"جدول دانلود: {json.dumps(t)[:250] if t else 'خالی'}")
    except Exception as e:
        L.append(f"جدول خطا: {type(e).__name__} {e}")

    short = None
    try:
        short = await asyncio.to_thread(
            SITES["anikoto"].short_link, ep, height, kind)
        L.append(f"short: {short}")
    except Exception as e:
        L.append(f"short خطا: {type(e).__name__} {e}")

    try:
        u = await asyncio.to_thread(
            SITES["anikoto"].ajax_source, ep["url"], kind)
        L.append(f"ajax_server: {u}")
    except Exception as e:
        L.append(f"ajax خطا: {type(e).__name__}")

    if short:
        b = await _get_browser()
        ctx = await b.new_context(user_agent=UA, locale="en-US")
        pg = await ctx.new_page()
        try:
            await pg.goto(short, referer="https://pahe.nekostream.site/",
                          wait_until="domcontentloaded", timeout=45000)
            await pg.wait_for_timeout(3500)
            if "pahe." in pg.url:
                for sel in ("button.btn", "button"):
                    try:
                        await pg.click(sel, timeout=3000)
                        break
                    except Exception:
                        continue
                await pg.wait_for_timeout(2500)
            L.append(f"landed: {pg.url[:90]}")
            if "kwik.cx" in pg.url:
                got = await SITES["anikoto"].kwik_mp4(pg)
                L.append(f"kwik_mp4: {str(got)[:110]}")
                if not got:
                    body = await pg.evaluate("() => document.body.innerText")
                    L.append("page: " + " ".join((body or "").split())[:180])
        except Exception as e:
            L.append(f"browser خطا: {type(e).__name__} {e}")
        finally:
            await ctx.close()

    return "\n".join(L)


def download(url, ref, kind, height, outdir, on_progress=None):
    os.makedirs(outdir, exist_ok=True)
    tmpl = os.path.join(outdir, "ep.%(ext)s")
    hdrs = {}
    if ref:
        hdrs["Referer"] = ref
        hdrs["Origin"] = origin(ref)
    hdrs.update(HDRS.get(url, {}))

    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--no-cache-dir",
           "--newline", "--retries", "5", "--fragment-retries", "20",
           "--concurrent-fragments", "8", "--no-check-certificates",
           "--socket-timeout", "30",
           "-f", f"bv*[height<={height}]+ba/b[height<={height}]/b",
           "--merge-output-format", "mp4", "--remux-video", "mp4",
           "-o", tmpl]
    for k, v in hdrs.items():
        cmd += ["--add-header", f"{k}: {v}"]
    if kind == "sub":
        cmd += ["--write-subs", "--write-auto-subs",
                "--sub-langs", "en,en-US,eng", "--convert-subs", "srt"]
    cmd.append(url)

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    for line in p.stdout:
        tail.append(line)
        if len(tail) > 80:
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
        hl = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
        r2 = subprocess.run(["ffmpeg", "-y", "-headers", hl,
                             "-i", url, "-c", "copy", out],
                            capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError(f"{origin(url)} → {''.join(tail)[-900:]}")
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
                        "-movflags", "+faststart", out], capture_output=True)
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
