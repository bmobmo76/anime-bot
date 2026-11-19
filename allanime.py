"""
AllAnime / MKissa provider.

Crypto comes from the auto-updated keygen JSON, so nothing is scraped from the
site and no browser is needed. Search is unsigned; episode sources are signed.

    search(query)          -> [{id, name, sub, dub}]
    episodes(show_id, kind)-> [1, 2, 3, ...]
    resolve(show_id, ep, kind) -> (url, referer, resolution)

CLI: python allanime.py "blue box" 2 sub
"""

import base64
import hashlib
import json
import re
import time
from urllib.parse import urljoin

from curl_cffi import requests as creq

API = "https://api.mkissa.net/api"
CDN_CLOCK = "https://allanime.day"
KEYGEN_URL = ("https://raw.githubusercontent.com/sdaqo/anipy-cli/"
              "refs/heads/key-gen/scripts/keygen/keygen.json")
ORIGIN = "https://mkissa.to"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SEARCH_QH = "a24c500a1b765c68ae1d8dd85174931f661c71369c89b92b88b75a725afc471c"
SHOW_QH = "043448386c7a686bc2aabfbb6b80f6074e795d350df48015023b079527b0848a"

ALLOWED = ["Yt-mp4", "S-Mp4", "Uv-mp4", "Luf-Mp4", "Ak", "Default", "Mp4"]

_keygen_cache = {"at": 0.0, "data": None}


def _sess():
    s = creq.Session(impersonate="chrome")
    s.headers.update({
        "User-Agent": UA,
        "Referer": ORIGIN + "/",
        "Origin": ORIGIN,
        "Accept": "application/json, text/plain, */*",
    })
    return s


def _keygen(sess):
    if _keygen_cache["data"] and time.time() - _keygen_cache["at"] < 600:
        return _keygen_cache["data"]
    r = sess.get(KEYGEN_URL, timeout=30)
    r.raise_for_status()
    data = r.json()
    _keygen_cache.update(at=time.time(), data=data)
    return data


def _aareq(kg):
    ts = int(time.time() * 1000) // 300000 * 300000
    payload = {
        "v": 1,
        "ts": ts,
        "epoch": kg["epoch"],
        "buildId": kg["build_id"],
        "qh": kg["query_hash"],
        "k": kg["lane"],
    }
    seed = f"{kg['epoch']}:{kg['query_hash']}:{ts}"
    iv = hashlib.sha256(seed.encode()).digest()[:12]
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ct = AESGCM(bytes.fromhex(kg["key"])).encrypt(
        iv, json.dumps(payload, separators=(",", ":")).encode(), None
    )
    return base64.b64encode(b"\x01" + iv + ct).decode()


def _decode_tobeparsed(kg, blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(blob)
    iv, ct, tag = raw[1:13], raw[13:-16], raw[-16:]
    candidates = [bytes.fromhex(kg["key"])]
    static = kg.get("static_key") or ""
    if static:
        candidates.append(static.encode())
        candidates.append(hashlib.sha256(static.encode()).digest())
    for key in candidates:
        if len(key) not in (16, 24, 32):
            continue
        try:
            plain = AESGCM(key).decrypt(iv, ct + tag, None)
            return json.loads(plain.decode("utf-8"))
        except Exception:
            continue
    _keygen_cache["at"] = 0.0
    return None


def _decrypt_path(value):
    out = ""
    for i in range(0, len(value) - 1, 2):
        try:
            num = int(value[i:i + 2], 16) ^ 56
        except ValueError:
            continue
        out += chr(int(oct(num)[2:].zfill(3), 8))
    return out


def _post(sess, variables, qh):
    params = {
        "variables": json.dumps(variables),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": qh}}),
    }
    r = sess.post(API, params=params, timeout=45)
    return r.json()


def search(query, limit=26):
    sess = _sess()
    body = {
        "query": ("query($search:SearchInput,$limit:Int,$page:Int)"
                  "{shows(search:$search,limit:$limit,page:$page)"
                  "{edges{_id name availableEpisodes thumbnail}}}"),
        "variables": {"search": {"query": query}, "limit": limit, "page": 1},
    }
    r = sess.post(API, data=json.dumps(body),
                  headers={"Content-Type": "application/json"}, timeout=45)
    edges = (((r.json().get("data") or {}).get("shows") or {}).get("edges")) or []
    out = []
    for e in edges:
        av = e.get("availableEpisodes") or {}
        out.append({
            "id": e.get("_id"),
            "name": e.get("name") or "",
            "sub": int(av.get("sub") or 0),
            "dub": int(av.get("dub") or 0),
            "thumb": e.get("thumbnail") or "",
        })
    return out


def episodes(show_id, kind="sub"):
    sess = _sess()
    try:
        data = _post(sess, {"_id": show_id}, SHOW_QH)
        detail = (data.get("data") or {}).get("show") or {}
        lst = ((detail.get("availableEpisodesDetail") or {}).get(kind)) or []
        if lst:
            return sorted(int(float(x)) for x in lst)
    except Exception:
        pass
    return []


def resolve(show_id, episode, kind="sub"):
    sess = _sess()
    kg = _keygen(sess)
    tt = "dub" if kind == "dub" else "sub"
    params = {
        "variables": json.dumps({
            "showId": show_id,
            "translationType": tt,
            "episodeString": str(episode),
        }),
        "extensions": json.dumps({
            "persistedQuery": {"version": 1, "sha256Hash": kg["query_hash"]},
            "aaReq": _aareq(kg),
            "k": kg["lane"],
        }),
    }
    r = sess.get(API, params=params, timeout=60,
                 headers={"x-build-id": kg["build_id"]})
    data = r.json().get("data") or {}

    if "tobeparsed" in data:
        data = _decode_tobeparsed(kg, data["tobeparsed"]) or {}
    if not data.get("episode"):
        raise RuntimeError("episode payload empty")

    streams = []
    for item in data["episode"].get("sourceUrls") or []:
        name = item.get("sourceName") or ""
        url = item.get("sourceUrl") or ""
        if name not in ALLOWED:
            continue

        if "tools.fast4speed.rsvp" in url:
            streams.append((1080, url, ORIGIN + "/"))
            continue

        if name == "Mp4":
            try:
                page = sess.get(url, timeout=45).text
            except Exception:
                continue
            m = re.search(r'src:\s*"([^"]+)"', page)
            if m:
                streams.append((1080, m.group(1), "https://www.mp4upload.com"))
            continue

        path = _decrypt_path(url.replace("--", "")).replace("clock", "clock.json")
        try:
            raw = sess.get(CDN_CLOCK + path, timeout=45,
                           headers={"Referer": CDN_CLOCK + "/"}).json()
        except Exception:
            continue

        for links in raw.get("links") or []:
            link = links.get("link") or ""
            if "repackager.wixmp.com" in link:
                parts = link.split(".urlset")[0].replace(
                    "repackager.wixmp.com/", "").split(",")
                head, tail = parts[0], parts[-1]
                for q in parts[1:-1]:
                    try:
                        streams.append((int(q.replace("p", "")), head + q + tail, ORIGIN))
                    except ValueError:
                        pass
                continue
            referer = (links.get("headers") or {}).get("Referer") or ORIGIN
            streams.append((1080, link, referer))

    if not streams:
        raise RuntimeError("no usable stream")
    streams.sort(key=lambda s: s[0], reverse=True)
    return streams[0]


def health():
    try:
        sess = _sess()
        kg = _keygen(sess)
        return {"ok": True, "build": kg.get("build_id"), "epoch": kg.get("epoch")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "blue box"
    ep = sys.argv[2] if len(sys.argv) > 2 else "2"
    kind = sys.argv[3] if len(sys.argv) > 3 else "sub"
    print("health:", health())
    hits = search(q)
    print("hits:", len(hits), hits[:1])
    if hits:
        h = hits[-1]
        print("show:", h["id"], h["name"], "sub", h["sub"], "dub", h["dub"])
        print("eps:", episodes(h["id"], kind)[:5])
        try:
            res, url, ref = resolve(h["id"], ep, kind)
            print("OK", res, url)
            print("ref:", ref)
        except Exception as exc:
            print("FAIL", type(exc).__name__, exc)
