"""
AllAnime provider for the anime-bot.

Talks to the public AllAnime GraphQL API, which is NOT behind Cloudflare and
therefore works from datacenter IPs (GitHub Actions). Search needs no signing;
resolving episode sources needs the "aaReq" AES-GCM extension, implemented here.

Public API:
    search(query)                     -> list of dicts
    episodes(show_id, kind)           -> list of ints
    resolve(show_id, episode, kind)   -> (mp4_url, referer)
    health()                          -> dict for /debug

CLI self-test:
    python allanime.py "blue box" 2 sub
"""

import base64
import hashlib
import json
import re
import time
from urllib.parse import urljoin

from curl_cffi import requests as creq

API = "https://api.allanime.day/api"
SITE = "https://mkissa.to"
ORIGIN = "https://youtu-chan.com"
MASK = "b1a9a4d051988f1b1b12dbb747439d9bd64b09ea17835600a7eaa4de87c1ad87"

EP_QH = "d405d0edd690624b66baba3068e0edc3ac90f1597d898a1ec8db4e5c43c00fec"
BUILD_FALLBACKS = ["9", "44", "181"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": ORIGIN,
    "Referer": ORIGIN + "/",
}

SEARCH_QUERY = (
    'query($search:SearchInput,$limit:Int,$page:Int)'
    '{shows(search:$search,limit:$limit,page:$page)'
    '{edges{_id name availableEpisodes thumbnail}}'
    '}'
)

CIPHER = {
    0x79: "A", 0x7A: "B", 0x7B: "C", 0x7C: "D", 0x7D: "E", 0x7E: "F", 0x7F: "G",
    0x70: "H", 0x71: "I", 0x72: "J", 0x73: "K", 0x74: "L", 0x75: "M", 0x76: "N",
    0x77: "O", 0x68: "P", 0x69: "Q", 0x6A: "R", 0x6B: "S", 0x6C: "T", 0x6D: "U",
    0x6E: "V", 0x6F: "W", 0x60: "X", 0x61: "Y", 0x62: "Z", 0x59: "a", 0x5A: "b",
    0x5B: "c", 0x5C: "d", 0x5D: "e", 0x5E: "f", 0x5F: "g", 0x50: "h", 0x51: "i",
    0x52: "j", 0x53: "k", 0x54: "l", 0x55: "m", 0x56: "n", 0x57: "o", 0x48: "p",
    0x49: "q", 0x4A: "r", 0x4B: "s", 0x4C: "t", 0x4D: "u", 0x4E: "v", 0x4F: "w",
    0x40: "x", 0x41: "y", 0x42: "z", 0x08: "0", 0x09: "1", 0x0A: "2", 0x0B: "3",
    0x0C: "4", 0x0D: "5", 0x0E: "6", 0x0F: "7", 0x00: "8", 0x01: "9", 0x02: ":",
    0x03: "/", 0x04: ".", 0x05: "-", 0x06: "_", 0x07: "?", 0x1B: "&", 0x1C: "=",
    0x1D: "%", 0x1E: "$", 0x1F: ",", 0x10: " ", 0x11: "(", 0x12: ")", 0x13: "[",
    0x14: "]", 0x15: "{", 0x16: "}", 0x17: "|", 0x18: "#", 0x19: ";", 0x1A: "+",
    0x20: "@", 0x21: "!", 0x22: "~", 0x23: "`", 0x24: "'", 0x25: '"', 0x26: "*",
    0x27: "<", 0x28: ">", 0x29: "^",
}


def _sess():
    s = creq.Session(impersonate="chrome")
    s.headers.update(HEADERS)
    return s


def _decipher(url):
    """Decode an obfuscated '--xxxx' AllAnime URL."""
    if not url:
        return url
    if not url.startswith("--"):
        return url
    body = url[2:]
    out = []
    for i in range(0, len(body) - 1, 2):
        try:
            out.append(CIPHER.get(int(body[i:i + 2], 16), ""))
        except ValueError:
            pass
    return "".join(out)


def _xor_key(part_b):
    raw = base64.b64decode(part_b)
    mask = bytes.fromhex(MASK)
    return bytes(a ^ b for a, b in zip(raw, mask * 4))


def _aes_gcm_encrypt(key, iv, plain):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ct = AESGCM(key).encrypt(iv, plain, None)
    return ct


def _aes_ctr_decrypt(key, iv, data):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    c = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return c.update(data) + c.finalize()


def _decrypt_blob(key, blob):
    """Best-effort decrypt of the 'tobeparsed' field. Returns str or None."""
    try:
        raw = base64.b64decode(blob)
    except Exception:
        return None
    attempts = []
    if raw[:1] == b"\x01" and len(raw) > 13:
        attempts.append(raw[1:13])
        attempts.append(raw[1:17])
    if len(raw) > 12:
        attempts.append(raw[:12])
        attempts.append(raw[:16])
    for ivlen in (12, 16):
        iv = (hashlib.sha256(key).digest()[:ivlen])
        attempts.append(iv)
    for iv in attempts:
        if len(iv) not in (12, 16):
            continue
        body = raw[1 + len(iv):] if raw[:1] == b"\x01" else raw[len(iv):]
        try:
            out = _aes_ctr_decrypt(key, iv, body)
            txt = out.decode("utf-8", "ignore").strip()
            if txt.startswith("{") or '"sourceUrls"' in txt:
                return txt
        except Exception:
            continue
    return None


def _page_crypto(sess, show_id, episode, kind):
    """Scrape __aaCrypto (epoch/partB/buildId) from the episode page."""
    url = f"{SITE}/watch/{show_id}/p-{episode}-{kind}"
    r = sess.get(url, timeout=45)
    html = r.text or ""
    if r.status_code != 200:
        raise RuntimeError(f"crypto page HTTP {r.status_code}")
    epoch = re.search(r'"epoch"\s*:\s*(\d+)', html)
    part_b = re.search(r'"partB"\s*:\s*"([^"]+)"', html)
    build = re.search(r'"buildId"\s*:\s*"?(\d+)"?', html)
    if not epoch or not part_b:
        raise RuntimeError("__aaCrypto not found on page")
    return epoch.group(1), part_b.group(1), (build.group(1) if build else None)


def _signed_request(sess, show_id, episode, kind):
    epoch, part_b, build = _page_crypto(sess, show_id, episode, kind)
    key = _xor_key(part_b)
    builds = [build] if build else []
    builds += [b for b in BUILD_FALLBACKS if b not in builds]

    last = "unknown"
    for b in builds:
        ts = int(time.time() // 300 * 300)
        seed = f"{epoch}:{b}:{EP_QH}:{ts}"
        iv = hashlib.sha256(seed.encode()).digest()[:12]
        payload = json.dumps(
            {"v": 1, "ts": ts, "epoch": int(epoch), "buildId": b, "qh": EP_QH},
            separators=(",", ":"),
        ).encode()
        aa_req = base64.b64encode(b"\x01" + iv + _aes_gcm_encrypt(key, iv, payload)).decode()
        body = {
            "variables": {
                "showId": show_id,
                "translationType": kind,
                "episodeString": str(episode),
            },
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": EP_QH},
                "aaReq": aa_req,
            },
        }
        hdrs = {"Content-Type": "application/json", "x-build-id": b}
        r = sess.post(API, data=json.dumps(body), headers=hdrs, timeout=60)
        try:
            data = r.json()
        except Exception:
            last = f"HTTP {r.status_code} non-json"
            continue
        ep = (data.get("data") or {}).get("episode")
        if ep:
            return ep, key
        err = data.get("errors") or [{}]
        last = err[0].get("message", f"HTTP {r.status_code}")
    raise RuntimeError(f"episode query failed: {last}")


def _sources(ep, key):
    """Return [(name, url)] from an episode payload, decrypting if needed."""
    if isinstance(ep, dict) and isinstance(ep.get("sourceUrls"), list):
        return [(s.get("sourceName", ""), s.get("sourceUrl", "")) for s in ep["sourceUrls"]]
    blob = None
    if isinstance(ep, dict):
        for k in ("tobeparsed", "toBeParsed", "encrypted"):
            if isinstance(ep.get(k), str):
                blob = ep[k]
                break
    if not blob:
        return []
    txt = _decrypt_blob(key, blob)
    if not txt:
        return []
    try:
        obj = json.loads(txt)
    except Exception:
        return []
    if isinstance(obj, dict) and isinstance(obj.get("sourceUrls"), list):
        obj = obj["sourceUrls"]
    if not isinstance(obj, list):
        return []
    out = []
    for s in obj:
        if isinstance(s, dict):
            out.append((s.get("sourceName", ""), s.get("sourceUrl", "")))
    return out


def _clock(sess, url):
    """Resolve an /apivtwo/ or /apiv2/ clock endpoint to a direct mp4."""
    r = sess.get(url, timeout=60, headers={"Referer": ORIGIN + "/"})
    try:
        data = r.json()
    except Exception:
        return None
    links = data.get("links") or []
    best = None
    for ln in links:
        link = ln.get("link") or ""
        if not link.startswith("http"):
            continue
        if ".m3u8" in link:
            continue
        try:
            res = int(ln.get("resolutionStr") or ln.get("resolution") or 0)
        except (TypeError, ValueError):
            res = 0
        if best is None or res > best[0]:
            best = (res, link)
    return best[1] if best else None


def search(query, limit=25):
    sess = _sess()
    body = {
        "query": SEARCH_QUERY,
        "variables": {"search": {"query": query}, "limit": limit, "page": 1},
    }
    r = sess.post(API, data=json.dumps(body),
                  headers={"Content-Type": "application/json"}, timeout=45)
    data = r.json()
    edges = (((data.get("data") or {}).get("shows") or {}).get("edges")) or []
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
        "query": SEARCH_QUERY,
        "variables": {"search": {"query": ""}, "limit": 1, "page": 1},
    }
    r = sess.post(API, data=json.dumps(body),
                  headers={"Content-Type": "application/json"}, timeout=45)
    edges = (((r.json().get("data") or {}).get("shows") or {}).get("edges")) or []
    if not edges:
        return []
    av = edges[0].get("availableEpisodes") or {}
    n = int(av.get(kind) or 0)
    return list(range(1, n + 1))


def resolve(show_id, episode, kind="sub"):
    sess = _sess()
    ep, key = _signed_request(sess, show_id, episode, kind)
    srcs = _sources(ep, key)
    if not srcs:
        raise RuntimeError("no sourceUrls")

    direct, clock = [], []
    for name, raw in srcs:
        url = _decipher(raw)
        if not url.startswith("http"):
            continue
        low = url.lower()
        if "fast4speed" in low or low.endswith(".mp4"):
            direct.append(url)
        elif "clock.json" in low or "/apiv" in low:
            clock.append(url)

    for url in direct:
        return url, ORIGIN + "/"
    for url in clock:
        mp4 = _clock(sess, url)
        if mp4:
            return mp4, ORIGIN + "/"
    raise RuntimeError(f"no playable link among {len(srcs)} sources")


def health():
    try:
        res = search("blue box", limit=3)
        return {"ok": bool(res), "results": len(res), "sample": res[:1]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "blue box"
    ep_no = sys.argv[2] if len(sys.argv) > 2 else "2"
    kind = sys.argv[3] if len(sys.argv) > 3 else "sub"
    print("health:", json.dumps(health(), ensure_ascii=False)[:400])
    hits = search(q)
    print("search:", json.dumps(hits[:3], ensure_ascii=False))
    if hits:
        target = hits[0]
        print("show:", target["id"], target["name"], "sub", target["sub"], "dub", target["dub"])
        try:
            url, ref = resolve(target["id"], ep_no, kind)
            print("mp4:", url)
            print("referer:", ref)
        except Exception as exc:
            print("resolve failed:", type(exc).__name__, exc)
