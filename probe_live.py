import json
import traceback

import keygen_live as kl

try:
    data = kl.fetch_live()
    print("OK build:", data["build_id"], "epoch:", data["epoch"], "lane:", data["lane"])
    print("key len:", len(data["key"]) // 2, "hash:", data["query_hash"])
except Exception:
    traceback.print_exc()
