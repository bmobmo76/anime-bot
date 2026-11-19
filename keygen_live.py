"""Fetch live MKissa crypto values (epoch, partB-derived key, build, lane, hash).

Needs node + npm on the runner; the mask is extracted from the site bundle with
the public mbpowers/ani-extract tool, exactly like upstream's keygen does.
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
    chunk_path = os.path.join(tool, "chunk.js")
    with open(chunk_path, "w", encoding="utf-8") as fh:
        fh.write(chunk_js)
    res = subprocess.run(["node", "ani-extract.js", "./chunk.js"],
                         cwd=tool, capture_output=True, text=True, timeout=120)
    blocks = json.loads(res.stdout.strip().replace("'", '"'))
    embedded = b"".join(base64.b64decode(b) for b in blocks)
    return embedded


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
    """Return dict with build_id, epoch, lane, key, query_hash."""
    s = _session()
    html = s.get(MKISSA, timeout=20).text
    app_path = re.search(r"/entry/app\.[A-Za-z0-9_.-]+\.js", html)
    if not app_path:
        raise RuntimeError("entry bundle not found")
    app_js = s.get(CDN + app_path.group(), timeout=20).text

    chunks = re.findall(r'"../(chunks/[A-Za-z0-9_.-]+\.js)"', app_js)[:5]
    for chunk in chunks:
        chunk_js = s.get(f"{CDN}/{chunk}", timeout=20).text
        if "VaildTranslationTypeEnumType" not in chunk_js and "x-aa-boot" not in chunk_js:
            continue

        build = re.search(r'!=="string"\?"([0-9]+)"', chunk_js)
        lane = re.search(r'const ..="(k[0-9]+)"', chunk_js)
        if not build or not lane:
            continue
        build_id, lane_id = build.group(1), lane.group(1)

        now = int(time.time() * 1000)
        epoch = now // EPOCH_MS
        if now - epoch * EPOCH_MS < 86_400_000 and epoch > 0:
            epoch -= 1

        embedded = _mask_from_chunk(chunk_js)
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

        boot = s.get(f"{API}/client-crypto/v1/bootstrap",
                     params={"buildId": build_id, "k": lane_id},
                     headers={
                         "x-build-id": build_id,
                         "x-aa-boot": aa_boot,
                         "Referer": MKISSA,
                         "Origin": MKISSA,
                     }, timeout=20).json()

        key = bytes(a ^ b for a, b in zip(mask, base64.b64decode(boot["partB"])))
        return {
            "build_id": build_id,
            "epoch": boot["epoch"],
            "lane": boot.get("k", lane_id),
            "key": key.hex(),
            "query_hash": _query_hash(chunk_js),
            "static_key": "Xot36i3lK3:v1",
        }

    raise RuntimeError("no crypto chunk found")


if __name__ == "__main__":
    print(json.dumps(fetch_live(), indent=2))
