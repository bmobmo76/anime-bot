import inspect
import traceback

import anipy_api
from anipy_api.provider import get_provider, list_providers

print("version:", getattr(anipy_api, "__version__", "?"))

print("\n=== get_provider signature ===")
try:
    print(inspect.signature(get_provider))
except Exception as exc:
    print("sig err", exc)
try:
    print(inspect.getsource(get_provider)[:1200])
except Exception as exc:
    print("src err", exc)

print("\n=== providers ===")
for cls in list_providers():
    print("\n---", cls.__name__)
    try:
        print("module:", cls.__module__)
        print("sig:", inspect.signature(cls))
    except Exception as exc:
        print("sig err", exc)
    try:
        print("init src:", inspect.getsource(cls.__init__)[:700])
    except Exception as exc:
        print("src err", exc)

print("\n=== provider base ===")
try:
    from anipy_api.provider.base import Provider
    print([m for m in dir(Provider) if not m.startswith("_")])
    for name in ("get_search_results", "get_episodes", "get_video", "get_info"):
        fn = getattr(Provider, name, None)
        if fn:
            try:
                print(name, inspect.signature(fn))
            except Exception:
                pass
except Exception as exc:
    print("base err", exc)

print("\n=== LanguageTypeEnum ===")
try:
    from anipy_api.provider import LanguageTypeEnum
    print(list(LanguageTypeEnum))
except Exception as exc:
    print("enum err", exc)

print("\n=== try instantiate + search ===")
for cls in list_providers():
    name = cls.__name__
    print("\n---", name)
    try:
        p = cls()
    except Exception as exc:
        print("instantiate fail:", type(exc).__name__, exc)
        continue
    try:
        res = list(p.get_search_results("blue box"))[:2]
        print("search ok:", res)
        if not res:
            continue
        ident = getattr(res[0], "identifier", None)
        print("identifier:", ident)
        eps = list(p.get_episodes(ident, LanguageTypeEnum.SUB))[:3]
        print("episodes:", eps)
        if eps:
            st = p.get_video(ident, eps[1] if len(eps) > 1 else eps[0], LanguageTypeEnum.SUB)
            print("STREAM:", getattr(st, "url", st))
            print("referrer:", getattr(st, "referrer", None))
            print("resolution:", getattr(st, "resolution", None))
            break
    except Exception as exc:
        print("fail:", type(exc).__name__, exc)
        traceback.print_exc(limit=2)
