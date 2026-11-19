"""Fetch live MKissa crypto values (epoch, partB-derived key, build, lane, hash).

The mask is extracted from the site bundle with mbpowers/ani-extract (node).
"""

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
import time

import requests

MKISSA = "https://mkissa.to/"
API = "https://api.mkissa.net"
CDN = "https://cdn.mkissa.net/all/mk/_app/immutable"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
EPOCH_MS = 259_200_000

_tool_dir = {"path": None}


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def _tool():
    if _tool_dir["path"]:
        return _tool_dir["path"]
    path = os.path.join(tempfile.gettempdir(), "ani-extract")
    if not os.path.isdir(path):
        subprocess.run(
            ["git", "clone", "--depth", "1",
             "https://github.com/mbpowers/ani-extract", path],
            check=True, capture_output=True, timeout=180,
        )
        subprocess.run(["npm", "i", "--silent"], cwd=path,
                       check=True, capture_output=True, timeout=300)
    _tool_dir["path"] = path
    return path


def _mask_from_chunk(chunk_js):
    tool = _tool()
    with open(os.path.join(tool, "chunk.js"), "w", encoding="utf-8") as fh:
        fh.write(chunk_js)
    res = subprocess.run(["node", "ani-extract.js", "./chunk.js"],
                         cwd=tool, capture_output=True, text=True, timeout=120)
    blocks = json.loads(res.stdout.strip().replace("'", '"'))
    return b"".join(base64.b64decode(b) for b in blocks)


def _build_candidates(chunk_js):
    found = []
    for pattern in (
        r'!==\s*"string"\s*\?\s*"(\d{1,4})"',
        r'typeof[^?]{0,60}\?\s*"(\d{1,4})"\s*:',
        r'buildId[^,;{}]{0,60}?"(\d{1,4})"',
        r'"(\d{1,4})"[^,;{}]{0,40}?buildId',
    ):
        for m in re.finditer(pattern, chunk_js):
            if m.group(1) not in found:
                found.append(m.group(1))
    for anchor in ("x-aa-boot", "x-build-id"):
        idx = chunk_js.find(anchor)
        if idx == -1:
            continue
        window = chunk_js[max(0, idx - 600): idx + 600]
        for m in re.finditer(r'"(\d{1,4})"', window):
            if m.group(1) not in found:
                found.append(m.group(1))
    return [b for b in found if 1 <= int(b) <= 9999]


def _lane(chunk_js):
    m = re.search(r'"(k\d{1,3})"', chunk_js)
    return m.group(1) if m else "k7"


def _query_hash(chunk_js):
    template = next((t for t in re.findall(r"(\nquery\([^`]*)`", chunk_js)
                     if "sourceUrls" in t and "episode(" in t), None)
    if template is None:
        return None

    def resolve(tmpl, depth=0):
        if depth > 6:
            return tmpl
        for name in re.findall(r"\$\{([^}]+)\}", tmpl):
            if name.endswith("()"):
                fn = re.search(re.escape(name[:-2])
                               + r"\s*=\s*\w+\s*=>\s*\w+\s*\?\s*`[^`]*`\s*:\s*`([^`]*)`",
                               chunk_js)
                repl = resolve(fn.group(1), depth + 1) if fn else ""
            else:
                var = re.search(re.escape(name) + r"\s*=\s*`([^`]*)`", chunk_js)
                repl = resolve(var.group(1), depth + 1) if var else ""
            tmpl = tmpl.replace("${" + name + "}", repl)
        return tmpl

    query = resolve(template)
    if "${" in query:
        return None
    return hashlib.sha256(query.encode()).hexdigest()


def fetch_live():
    s = _session()
    html = s.get(MKISSA, timeout=20).text
    app_path = re.search(r"/entry/app\.[A-Za-z0-9_.-]+\.js", html)
    if not app_path:
        raise RuntimeError("entry bundle not found")
    app_js = s.get(CDN + app_path.group(), timeout=20).text

    chunks = re.findall(r'"../(chunks/[A-Za-z0-9_.-]+\.js)"', app_js)[:5]
    errors = []
    for chunk in chunks:
        chunk_js = s.get(f"{CDN}/{chunk}", timeout=30).text
        if "VaildTranslationTypeEnumType" not in chunk_js and "x-aa-boot" not in chunk_js:
            continue

        lane_id = _lane(chunk_js)
        embedded = _mask_from_chunk(chunk_js)
        if len(embedded) < 32:
            errors.append(f"{chunk}: mask too short ({len(embedded)})")
            continue

        now = int(time.time() * 1000)
        epoch = now // EPOCH_MS
        if now - epoch * EPOCH_MS < 86_400_000 and epoch > 0:
            epoch -= 1

        for build_id in _build_candidates(chunk_js)[:6]:
            mask = bytes(
                value
                ^ (ord(build_id[i % len(build_id)]) ^ ((i * 17 + 31) & 0xFF))
                ^ ((i // 8 * 41 + i % 8 * 7) & 0xFF)
                for i, value in enumerate(embedded)
            )
            hk = hmac.digest(mask, f"aa-boot:{build_id}".encode(), "sha256")
            aa_boot = hmac.digest(
                hk, f"{build_id}:mkissa:mkissa.to:{epoch}:{lane_id}".encode(), "sha256"
            ).hex()
            r = s.get(f"{API}/client-crypto/v1/bootstrap",
                      params={"buildId": build_id, "k": lane_id},
                      headers={
                          "x-build-id": build_id,
                          "x-aa-boot": aa_boot,
                          "Referer": MKISSA,
                          "Origin": MKISSA,
                      }, timeout=20)
            try:
                boot = r.json()
            except Exception:
                errors.append(f"build {build_id}: non-json {r.status_code}")
                continue
            if "partB" not in boot:
                errors.append(f"build {build_id}: {str(boot)[:120]}")
                continue

            key = bytes(a ^ b for a, b in zip(mask, base64.b64decode(boot["partB"])))
            return {
                "build_id": build_id,
                "epoch": boot.get("epoch", epoch),
                "lane": boot.get("k", lane_id),
                "key": key.hex(),
                "query_hash": _query_hash(chunk_js),
                "static_key": "Xot36i3lK3:v1",
            }

    raise RuntimeError("bootstrap failed: " + "; ".join(errors[:6]))


if __name__ == "__main__":
    print(json.dumps(fetch_live(), indent=2))
