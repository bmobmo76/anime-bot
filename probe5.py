import json

import allanime as aa

s = aa._sess()
kg = aa._keygen(s)
print("keygen build:", kg["build_id"], "lane:", kg["lane"])

SHOW = "mgL8CshEj25LXz273"

print("\n=== show query (POST json-string) ===")
body = {
    "variables": json.dumps({"_id": SHOW}),
    "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": aa.SHOW_QH}}),
}
r = s.post(aa.API, json=body, timeout=30)
print(r.status_code, r.text[:400])

print("\n=== show query (POST params) ===")
r = s.post(aa.API, params=body, timeout=30)
print(r.status_code, r.text[:400])

params = {
    "variables": json.dumps({"showId": SHOW, "translationType": "sub", "episodeString": "2"}),
    "extensions": json.dumps({
        "persistedQuery": {"version": 1, "sha256Hash": kg["query_hash"]},
        "aaReq": aa._aareq(kg),
        "k": kg["lane"],
    }),
}

print("\n=== episode GET ===")
r = s.get(aa.API, params=params, timeout=30, headers={"x-build-id": kg["build_id"]})
print(r.status_code, r.text[:500])

print("\n=== episode GET (no x-build-id) ===")
r = s.get(aa.API, params=params, timeout=30)
print(r.status_code, r.text[:300])

print("\n=== episode GET (Referer allmanga.to) ===")
r = s.get(aa.API, params=params, timeout=30,
          headers={"x-build-id": kg["build_id"], "Referer": "https://allmanga.to/"})
print(r.status_code, r.text[:300])
