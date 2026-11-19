"""
AllAnime / MKissa provider.
Crypto comes from the auto-updated keygen JSON. No browser needed.

    search(query)              -> [{id, name, sub, dub, thumb}]
    episodes(show_id, kind)    -> [1, 2, 3, ...]
    resolve(show_id, ep, kind) -> (resolution, url, referer)
    health()                   -> dict
    debug(show_id, ep, kind)   -> raw diagnostics

CLI: python allanime.py "blue box" 2 sub
"""

import base64
import hashlib
import json
import re
import time

from curl_cffi import requests as creq

API = "https://api.mkissa.net/api"
CDN_CLOCK = "https://allanime.day"
KEYGEN_URL = ("https://raw.githubusercontent.com/sdaqo/anipy-cli/"
              "refs/heads/key-gen/scripts/keygen/keygen.json")
ORIGIN = "https://mkissa.to"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SHOW_QH = "043448386c7a686bc2aabfbb6b80f6074e795d350df48015023b079527b0848a"
ALLOWED = {"Yt-mp4", "S-Mp4", "Uv-mp4", "Luf-Mp4", "Ak", "Default", "Mp4"}

_keygen_cache = {"at": 0.0, "data": None}


def _sess():
    s = creq.Session(impersonate="chrome")
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://allmanga.to/",
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


def _keys(kg):
    """AES candidates: current key first, then legacy static key (raw and sha256)."""
    out = [bytes.fromhex(kg["key"])]
    static = (kg.get("static_key") or "").encode()
    if static:
        out.append(static)
        out.append(hashlib.sha256(static).digest())
    return [k for k in out if len(k) in (16, 24, 32)]


def _aareq(kg):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ts = int(time.time() * 1000) // 300000 * 300000
    payload = {
        "v": 1, "ts": ts, "epoch": kg["epoch"],
        "buildId": kg["build_id"], "qh": kg["query_hash"], "k": kg["lane"],
    }
    seed = f"{kg['epoch']}:{kg['query_hash']}:{ts}"
    iv = hashlib.sha256(seed.encode()).digest()[:12]
    ct = AESGCM(bytes.fromhex(kg["key"])).encrypt(
        iv, json.dumps(payload, separators=(",", ":")).encode(), None
    )
    return base64.b64encode(b"\x01" + iv + ct).decode()


def _decode_tobeparsed(kg, blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(blob)
    if len(raw) < 30:
        return None, "blob too short"
    iv, body, tag = raw[1:13], raw[13:-16], raw[-16:]
    for key in _keys(kg):
        try:
            plain = AESGCM(key).decrypt(iv, body + tag, None)
            return json.loads(plain.decode("utf-8")), None
        except Exception:
            continue
    _keygen_cache["at"] = 0.0
    return None, "all keys failed"


def _decrypt_path(value):
    out = ""
    for i in range(0, len(value) - 1, 2):
        try:
            num = int(value[i:i + 2], 16) ^ 56
        except ValueError:
            continue
        out += chr(int(oct(num)[2:].zfill(3), 8))
    return out


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
    body = {
        "variables": json.dumps({"_id": show_id}),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": SHOW_QH}}),
    }
    r = sess.post(API, data=json.dumps(body),
                  headers={"Content-Type": "application/json"}, timeout=45)
    detail = ((r.json().get("data") or {}).get("show") or {})
    lst = ((detail.get("availableEpisodesDetail") or {}).get(kind)) or []
    return sorted(int(float(x)) for x in lst)


def debug(show_id, episode, kind="sub"):
    sess = _sess()
    kg = _keygen(sess)
    out = {"build": kg.get("build_id"), "lane": kg.get("lane")}
    body = {
        "variables": json.dumps({"_id": show_id}),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": SHOW_QH}}),
    }
    r = sess.post(API, data=json.dumps(body),
                  headers={"Content-Type": "application/json"}, timeout=45)
    out["show_status"] = r.status_code
    out["show_body"] = r.text[:300]

    params = {
        "variables": json.dumps({
            "showId": show_id,
            "translationType": "dub" if kind == "dub" else "sub",
            "episodeString": str(episode),
        }),
        "extensions": json.dumps({
            "persistedQuery": {"version": 1, "sha256Hash": kg["query_hash"]},
            "aaReq": _aareq(kg),
            "k": kg["lane"],
        }),
    }
    r = sess.get(API, params=params, timeout=60, headers={
        "x-build-id": kg["build_id"], "Referer": ORIGIN, "Origin": ORIGIN,
    })
    out["ep_status"] = r.status_code
    try:
        payload = r.json()
    except Exception:
        out["ep_body"] = r.text[:300]
        return out
    data = payload.get("data") or {}
    out["errors"] = payload.get("errors")
    out["has_tobeparsed"] = "tobeparsed" in data
    if "tobeparsed" in data:
        out["blob_len"] = len(data["tobeparsed"])
        dec, err = _decode_tobeparsed(kg, data["tobeparsed"])
        out["decode_error"] = err
        if dec:
            out["decoded_keys"] = list(dec.keys())
            ep = dec.get("episode") or {}
            srcs = ep.get("sourceUrls") or []
            out["sources"] = [s.get("sourceName") for s in srcs][:10]
    return out


def resolve(show_id, episode, kind="sub"):
    sess = _sess()
    kg = _keygen(sess)
    params = {
        "variables": json.dumps({
            "showId": show_id,
            "translationType": "dub" if kind == "dub" else "sub",
            "episodeString": str(episode),
        }),
        "extensions": json.dumps({
            "persistedQuery": {"version": 1, "sha256Hash": kg["query_hash"]},
            "aaReq": _aareq(kg),
            "k": kg["lane"],
        }),
    }
    r = sess.get(API, params=params, timeout=60, headers={
        "x-build-id": kg["build_id"], "Referer": ORIGIN, "Origin": ORIGIN,
    })
    payload = r.json()
    data = payload.get("data") or {}
    if "tobeparsed" in data:
        data, err = _decode_tobeparsed(kg, data["tobeparsed"])
        if data is None:
            raise RuntimeError(f"decode failed: {err}")
    if not data.get("episode"):
        raise RuntimeError(f"no episode in payload: {str(payload)[:200]}")

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
        kg = _keygen(_sess())
        return {"ok": True, "build": kg.get("build_id")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "blue box"
    ep = sys.argv[2] if len(sys.argv) > 2 else "2"
    kind = sys.argv[3] if len(sys.argv) > 3 else "sub"
    print("health:", health())
    hits = search(q)
    print("hits:", len(hits))
    if hits:
        h = hits[-1]
        print("show:", h["id"], h["name"])
        print("eps:", episodes(h["id"], kind)[:6])
        print("debug:", json.dumps(debug(h["id"], ep, kind), ensure_ascii=False)[:900])
        try:
            res, url, ref = resolve(h["id"], ep, kind)
            print("OK", res, url)
            print("ref:", ref)
        except Exception as exc:
            print("FAIL", type(exc).__name__, exc)
