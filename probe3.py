import inspect
import traceback


def show(title, fn):
    print("\n===", title, "===")
    try:
        out = fn()
        print("OK:", repr(out)[:800])
        return out
    except Exception as exc:
        print("FAIL:", type(exc).__name__, exc)
        traceback.print_exc(limit=2)
        return None


try:
    import anipy_api
    from anipy_api.provider import get_provider, list_providers
except Exception:
    print("anipy_api import failed")
    traceback.print_exc()
    raise SystemExit(1)

print("anipy_api version:", getattr(anipy_api, "__version__", "?"))

provs = show("list_providers", lambda: list(list_providers()))
print("providers sample:", str(provs)[:400])

prov = None
for name in ("AllAnime", "allanime"):
    prov = show("get_provider " + name, lambda n=name: get_provider(n))
    if prov:
        break

if not prov:
    raise SystemExit(1)

print("\nprovider type:", type(prov))
print("methods:", [m for m in dir(prov) if not m.startswith("_")])

for attr in ("get_search_results", "get_episodes", "get_video", "get_info"):
    fn = getattr(prov, attr, None)
    if fn:
        try:
            print(attr, inspect.signature(fn))
        except Exception:
            pass

results = show("search blue box", lambda: list(prov.get_search_results("blue box"))[:3])
if not results:
    raise SystemExit(1)

r0 = results[0]
print("\nfirst result:", r0)
ident = getattr(r0, "identifier", None) or (r0.get("identifier") if isinstance(r0, dict) else None)
print("identifier:", ident)

from anipy_api.provider import LanguageTypeEnum

eps = show("episodes sub", lambda: list(prov.get_episodes(ident, LanguageTypeEnum.SUB))[:5])
print("episodes:", eps)

if eps:
    ep = eps[1] if len(eps) > 1 else eps[0]
    stream = show("get_video", lambda: prov.get_video(ident, ep, LanguageTypeEnum.SUB))
    if stream:
        print("\nSTREAM URL:", getattr(stream, "url", stream))
        for attr in ("referrer", "referer", "headers", "resolution"):
            print(attr, getattr(stream, attr, None))
