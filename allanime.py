"""
AllAnime / MKissa provider.

Crypto base values come from an auto-updated keygen JSON; the epoch is walked
forward until the API stops answering AA_CRYPTO_STALE. The working epoch is
remembered on disk so later runs start from it.

    search(query)              -> [{id, name, sub, dub, thumb}]
    episodes(show_id, kind)    -> [1, 2, ...]
    resolve(show_id, ep, kind) -> (resolution, url, referer)
    health() / debug(...)

CLI: python allanime.py "blue box" 2 sub
"""

import base64
import hashlib
import json
import os
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
HINT_FILE = os.path.join("data", "allanime_epoch.json")

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
    if _keygen_cache["data"] and time.time() - _keygen_cache["at"] < 900:
        return _keygen_cache["data"]
    data = None
    try:
        import keygen_live
        data = keygen_live.fetch_live()
        if not data.get("query_hash"):
            data = None
    except Exception:
        data = None
    if data is None:
        r = sess.get(KEYGEN_URL, timeout=30)
        r.raise_for_status()
        data = r.json()
    _keygen_cache.update(at=time.time(), data=data)
    return data


def _read_hint():
    try:
        with open(HINT_FILE, "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("epoch") or 0)
    except Exception:
        return 0


def _write_hint(epoch):
    try:
        os.makedirs("data", exist_ok=True)
        with open(HINT_FILE, "w", encoding="utf-8") as fh:
            json.dump({"epoch": epoch, "at": int(time.time())}, fh)
    except Exception:
        pass


def _keys(kg):
    out = [bytes.fromhex(kg["key"])]
    static = (kg.get("static_key") or "").encode()
    if static:
        out.append(static)
        out.append(hashlib.sha256(static).digest())
    return [k for k in out if len(k) in (16, 24, 32)]


def _aareq(kg, epoch, build):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ts = int(time.time() * 1000) // 300000 * 300000
    payload = {
        "v": 1, "ts": ts, "epoch": epoch,
        "buildId": str(build), "qh": kg["query_hash"], "k": kg["lane"],
    }
    seed = f"{epoch}:{kg['query_hash']}:{ts}"
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


def _one(sess, kg, show_id, episode, kind, epoch, build):
    params = {
        "variables": json.dumps({
            "showId": show_id,
            "translationType": "dub" if kind == "dub" else "sub",
            "episodeString": str(episode),
        }),
        "extensions": json.dumps({
            "persistedQuery": {"version": 1, "sha256Hash": kg["query_hash"]},
            "aaReq": _aareq(kg, epoch, build),
            "k": kg["lane"],
        }),
    }
    r = sess.get(API, params=params, timeout=45, headers={
        "x-build-id": str(build), "Referer": ORIGIN, "Origin": ORIGIN,
    })
    return r.json()


def _fetch_episode(sess, kg, show_id, episode, kind):
    """Walk the epoch forward until accepted. Returns (data, log)."""
    log = []
    base_epoch = max(int(kg["epoch"]), _read_hint())
    build = int(kg["build_id"])

    for step in range(0, 8):
        epoch = base_epoch + step
        for _try in range(4):
            try:
                payload = _one(sess, kg, show_id, episode, kind, epoch, build)
            except Exception as exc:
                log.append(f"{epoch} net:{type(exc).__name__}")
                time.sleep(2)
                continue

            data = payload.get("data") or {}
            if "tobeparsed" in data or data.get("episode"):
                _write_hint(epoch)
                log.append(f"{epoch} OK")
                return data, log

            msgs = [str(e.get("message")) for e in payload.get("errors") or []]
            first = msgs[0] if msgs else "empty response"
            if first.startswith("Too many requests"):
                wait = re.findall(r"\d+", first)
                time.sleep((int(wait[0]) if wait else 5) + 2)
                continue
            log.append(f"{epoch} {first}")
            time.sleep(2)
            break
    return None, log


def debug(show_id, episode, kind="sub"):
    sess = _sess()
    kg = _keygen(sess)
    data, log = _fetch_episode(sess, kg, show_id, episode, kind)
    out = {"hint": _read_hint(), "attempts": log[:12]}
    if data is None:
        return out
    if "tobeparsed" in data:
        dec, err = _decode_tobeparsed(kg, data["tobeparsed"])
        out["decode_error"] = err
        if dec:
            out["sources"] = [s.get("sourceName") for s in
                              (dec.get("episode") or {}).get("sourceUrls") or []][:12]
    else:
        out["sources"] = [s.get("sourceName") for s in
                          (data.get("episode") or {}).get("sourceUrls") or []][:12]
    return out


def resolve(show_id, episode, kind="sub"):
    sess = _sess()
    kg = _keygen(sess)
    data, log = _fetch_episode(sess, kg, show_id, episode, kind)
    if data is None:
        raise RuntimeError("no accepted epoch: " + " | ".join(log[:6]))

    if "tobeparsed" in data:
        data, err = _decode_tobeparsed(kg, data["tobeparsed"])
        if data is None:
            raise RuntimeError(f"decode failed: {err}")

    streams = []
    for item in (data.get("episode") or {}).get("sourceUrls") or []:
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
    print("hits:", len(hits))
    if hits:
        h = hits[-1]
        print("show:", h["id"], h["name"])
        print("debug:", json.dumps(debug(h["id"], ep, kind), ensure_ascii=False)[:700])
        try:
            res, url, ref = resolve(h["id"], ep, kind)
            print("OK", res, url)
            print("ref:", ref)
        except Exception as exc:
            print("FAIL", type(exc).__name__, exc)
