import asyncio, json, os, pathlib, shutil, time, traceback
import httpx
import engine

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
RUN_SECONDS = 5.2 * 3600
START = time.time()
LAST_ERROR = "بدون خطا"

DATA = pathlib.Path("data")
DATA.mkdir(exist_ok=True)
STATE_F = DATA / "state.json"

state = {"offset": 0, "jobs": {}, "seq": 0, "set": {}, "cancel": []}
if STATE_F.exists():
    try:
        state.update(json.loads(STATE_F.read_text()))
    except Exception:
        pass

def save():
    try:
        STATE_F.write_text(json.dumps(state))
    except Exception:
        pass

def cfg(chat_id):
    c = state["set"].setdefault(str(chat_id), {})
    c.setdefault("q", 720)
    c.setdefault("m", "video")
    return c

def new_job(chat_id, payload):
    state["seq"] += 1
    jid = str(state["seq"])
    payload.update(chat_id=chat_id, jid=jid)
    state["jobs"][jid] = payload
    save()
    return jid

client = httpx.AsyncClient(timeout=60)

async def api(method, files=None, **params):
    try:
        r = await client.post(f"{API}/{method}", data=params, files=files, timeout=600)
        return r.json()
    except Exception as e:
        return {"ok": False, "desc": str(e)}

async def send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text[:4000]}
    if kb:
        p["reply_markup"] = json.dumps({"inline_keyboard": kb})
    return await api("sendMessage", **p)

async def edit(chat_id, mid, text, kb=None):
    p = {"chat_id": chat_id, "message_id": mid, "text": text[:4000]}
    if kb:
        p["reply_markup"] = json.dumps({"inline_keyboard": kb})
    r = await api("editMessageText", **p)
    return r if r.get("ok") else await send(chat_id, text, kb)

async def send_video(chat_id, path, caption, th=None):
    with open(path, "rb") as f:
        files = {"video": (os.path.basename(path), f, "video/mp4")}
        if th and os.path.exists(th):
            files["thumbnail"] = ("t.jpg", open(th, "rb"), "image/jpeg")
        return await api("sendVideo", files=files, chat_id=chat_id,
                         caption=caption, supports_streaming="true")

async def send_doc(chat_id, path, caption):
    with open(path, "rb") as f:
        files = {"document": (os.path.basename(path), f, "application/octet-stream")}
        return await api("sendDocument", files=files, chat_id=chat_id, caption=caption)

async def deliver(chat_id, path, caption, mode, th=None):
    if mode == "doc":
        return await send_doc(chat_id, path, caption)
    r = await send_video(chat_id, path, caption, th)
    if not r.get("ok"):
        r = await send_doc(chat_id, path, caption)
    return r

HELP = ("اسم انگلیسی انیمه را بفرست.\n"
        "بعد سایت، نسخه (زیرنویس/دوبله) و قسمت را انتخاب کن.\n"
        "می‌توانی یک قسمت، یک بازه یا همهٔ قسمت‌ها را بگیری.\n\n"
        "/settings کیفیت و نوع ارسال\n/queue کارهای جاری\n/cancel لغو")

def menu_kb():
    return [
        [{"text": "۴۸۰p", "callback_data": "s|q|480"},
         {"text": "۷۲۰p", "callback_data": "s|q|720"},
         {"text": "۱۰۸۰p", "callback_data": "s|q|1080"}],
        [{"text": "ارسال ویدیو", "callback_data": "s|m|video"},
         {"text": "ارسال فایل", "callback_data": "s|m|doc"}],
    ]

async def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/start"):
        return await send(chat_id, "سلام\n" + HELP)
    if text.startswith("/help"):
        return await send(chat_id, HELP)
    if text.startswith("/settings"):
        c = cfg(chat_id)
        return await send(chat_id, f"کیفیت: {c['q']}p\n"
                                   f"ارسال: {'ویدیو' if c['m'] == 'video' else 'فایل'}",
                        menu_kb())
    if text.startswith("/status"):
        up = int(time.time() - START)
        return await send(chat_id, f"فعال. {up // 60} دقیقه.\n"
                                   f"کارها: {len(state['jobs'])}\n"
                                   f"دیسک آزاد: {engine.free_gb():.1f}GB\n"
                                   f"آخرین خطا: {LAST_ERROR}")
    if text.startswith("/queue"):
        rows = [[{"text": f"{v['title'][:30]} E{n}",
                  "callback_data": f"j|{k}"}]
                for k, v in list(state["jobs"].items())[-10:]
                for n in [v.get("cur", "-")]]
        return await send(chat_id, f"{len(state['jobs'])} کار در جریان", rows or None)
    if text.startswith("/cancel"):
        state["cancel"].append(str(chat_id))
        save()
        return await send(chat_id, "لغو شد. کارهای باقی‌مانده رد می‌شوند.")
    if text.startswith("/debug"):
        await send(chat_id, "بررسی سایت‌ها...")
        return await send(chat_id, await engine.health())
    if text.startswith("/"):
        return

    await send(chat_id, f"جستجوی «{text}»...")
    res = await engine.search_all(text)
    if not res:
        res = await engine.search_all(text.replace(" ", ""))
    if not res:
        return await send(chat_id, "چیزی پیدا نشد. نام انگلیسی یا روماجی را امتحان کن.")
    jid = new_job(chat_id, {"type": "search", "results": res, "title": text})
    await show_results(chat_id, None, jid, 0)

async def show_results(chat_id, mid, jid, page):
    job = state["jobs"].get(jid)
    if not job:
        return await edit(chat_id, mid, "منقضی شد.")
    per = 8
    chunk = job["results"][page * per:(page + 1) * per]
    rows = [[{"text": f"[{engine.SITES[r['site']].name}] {r['title'][:52]}",
              "callback_data": f"a|{jid}|{job['results'].index(r)}"}]
            for r in chunk]
    nav = []
    if page > 0:
        nav.append({"text": "◀", "callback_data": f"g|{jid}|{page - 1}"})
    if (page + 1) * per < len(job["results"]):
        nav.append({"text": "▶", "callback_data": f"g|{jid}|{page + 1}"})
    if nav:
        rows.append(nav)
    await edit(chat_id, mid, f"نتایج ({len(job['results'])}):", rows)

async def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    await api("answerCallbackQuery", callback_query_id=cb["id"])
    p = cb["data"].split("|")
    a = p[0]

    if a == "s":
        c = cfg(chat_id)
        c[p[1]] = int(p[2]) if p[1] == "q" else p[2]
        save()
        return await edit(chat_id, mid,
                          f"ذخیره شد. کیفیت {c['q']}p / "
                          f"{'ویدیو' if c['m'] == 'video' else 'فایل'}", menu_kb())

    if a == "g":
        return await show_results(chat_id, mid, p[1], int(p[2]))

    if a == "a":
        job = state["jobs"].get(p[1])
        if not job:
            return await edit(chat_id, mid, "منقضی شد.")
        r = job["results"][int(p[2])]
        job.update(site=r["site"], anime_url=r["url"], title=r["title"])
        save()
        await edit(chat_id, mid, "بررسی نسخه‌های موجود...")
        try:
            eps = await asyncio.to_thread(engine.SITES[r["site"]].episodes, r["url"])
        except Exception as e:
            return await edit(chat_id, mid, f"لیست قسمت‌ها نیامد: {e}")
        if not eps:
            return await edit(chat_id, mid, "قسمتی پیدا نشد.")
        job["episodes"] = eps
        save()
        kb = [[{"text": "زیرنویس انگلیسی", "callback_data": f"k|{p[1]}|sub"}],
              [{"text": "دوبله انگلیسی", "callback_data": f"k|{p[1]}|dub"}]]
        await edit(chat_id, mid, f"{r['title']}\n{len(eps)} قسمت. کدام نسخه؟", kb)

    elif a == "k":
        job = state["jobs"].get(p[1])
        if not job:
            return await edit(chat_id, mid, "منقضی شد.")
        job["kind"] = p[2]
        save()
        await show_page(chat_id, mid, p[1], 0)

    elif a == "p":
        await show_page(chat_id, mid, p[1], int(p[2]))

    elif a == "e":
        job = state["jobs"].get(p[1])
        if not job:
            return await edit(chat_id, mid, "منقضی شد.")
        n = p[2]
        kb = [[{"text": "دانلود همین قسمت", "callback_data": f"d|{p[1]}|{n}"}],
              [{"text": "از این قسمت تا آخر", "callback_data": f"t|{p[1]}|{n}"}],
              [{"text": "همهٔ قسمت‌ها", "callback_data": f"x|{p[1]}"}]]
        await edit(chat_id, mid, f"{job['title']} — قسمت {n} ({job['kind']})\nچه چیزی؟", kb)

    elif a in ("d", "t", "x"):
        job = state["jobs"].get(p[1])
        if not job:
            return await edit(chat_id, mid, "منقضی شد.")
        if a == "d":
            nums = [p[2]]
        elif a == "t":
            alln = [e["num"] for e in job["episodes"]]
            nums = alln[alln.index(p[2]):]
        else:
            nums = [e["num"] for e in job["episodes"]]
        job["batch"] = nums
        save()
        await edit(chat_id, mid, f"شروع {len(nums)} قسمت...")
        asyncio.create_task(run_batch(job))

    elif a == "j":
        job = state["jobs"].get(p[1])
        if job:
            await edit(chat_id, mid, f"{job['title']} — {job.get('cur', 'در صف')}")

async def show_page(chat_id, mid, jid, page):
    job = state["jobs"].get(jid)
    if not job:
        return await edit(chat_id, mid, "منقضی شد.")
    eps = job["episodes"]
    per = 24
    chunk = eps[page * per:(page + 1) * per]
    rows, row = [], []
    for e in chunk:
        row.append({"text": e["num"], "callback_data": f"e|{jid}|{e['num']}"})
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append({"text": "◀", "callback_data": f"p|{jid}|{page - 1}"})
    if (page + 1) * per < len(eps):
        nav.append({"text": "▶", "callback_data": f"p|{jid}|{page + 1}"})
    if nav:
        rows.append(nav)
    await edit(chat_id, mid, f"{job['title']} — {job['kind']}\nقسمت:", rows)

sem = asyncio.Semaphore(2)

async def run_batch(job):
    for n in list(job.get("batch", [])):
        if str(job["chat_id"]) in state["cancel"]:
            state["cancel"].remove(str(job["chat_id"]))
            await send(job["chat_id"], "لغو شد.")
            break
        job["cur"] = n
        save()
        try:
            await one_ep(job, n)
        except Exception as e:
            LAST_ERROR = f"ep{n}: {str(e)[:150]}"
            await send(job["chat_id"], f"قسمت {n} نشد:\n{str(e)[:200]}")
        await asyncio.sleep(1)
    state["jobs"].pop(job["jid"], None)
    save()
    await send(job["chat_id"], "پایان کار.")

async def one_ep(job, num):
    async with sem:
        chat_id = job["chat_id"]
        c = cfg(chat_id)
        ep = next((e for e in job["episodes"] if e["num"] == num), None)
        if not ep:
            return
        if engine.free_gb() < 3:
            await send(chat_id, "دیسک پر شد؛ پاکسازی می‌کنم.")
            shutil.rmtree("/tmp/dl", ignore_errors=True)

        tmp = f"/tmp/dl/{job['jid']}-{num}"
        shutil.rmtree(tmp, ignore_errors=True)
        loop = asyncio.get_running_loop()
        msg = await send(chat_id, f"قسمت {num}: گرفتن لینک...")
        mid = msg.get("result", {}).get("message_id")

        try:
            url = await engine.resolve(job, ep, job["kind"])
        except Exception:
            url = None
        if not url:
            await edit(chat_id, mid, f"قسمت {num}: لینک درنیامد.")
            return

        def prog(pct):
            asyncio.run_coroutine_threadsafe(
                edit(chat_id, mid, f"قسمت {num}: دانلود {pct:.0f}%"), loop)

        try:
            video, subs = await loop.run_in_executor(
                None, engine.download, url, ep["url"], job["kind"], c["q"], tmp, prog)
        except Exception as e:
            await edit(chat_id, mid, f"قسمت {num}: دانلود نشد\n{str(e)[:200]}")
            shutil.rmtree(tmp, ignore_errors=True)
            return

        await edit(chat_id, mid, f"قسمت {num}: پردازش...")
        video = await loop.run_in_executor(None, engine.mux_subs, video, subs)
        parts = await loop.run_in_executor(None, engine.split, video)
        if len(parts) > 1:
            engine.cleanup(video)
        th = await loop.run_in_executor(None, engine.thumb, parts[0], tmp)

        base = f"{job['title']} - E{num} [{c['q']}p]"
        for i, f in enumerate(parts):
            cap = base + (f" ({i + 1}/{len(parts)})" if len(parts) > 1 else "")
            r = await deliver(chat_id, f, cap, c["m"], th)
            if not r.get("ok"):
                await send(chat_id, f"ارسال نشد: {r.get('desc')}")
            engine.cleanup(f)
            await asyncio.sleep(1)

        shutil.rmtree(tmp, ignore_errors=True)
        await edit(chat_id, mid, f"قسمت {num}: تمام.")

async def handle_update(u):
    global LAST_ERROR
    try:
        if "message" in u:
            await handle_message(u["message"])
        elif "callback_query" in u:
            await handle_callback(u["callback_query"])
    except Exception as e:
        LAST_ERROR = f"handler: {e}"
        print(traceback.format_exc())

async def poll():
    while time.time() - START < RUN_SECONDS:
        try:
            r = await client.get(f"{API}/getUpdates", timeout=60, params={
                "offset": state["offset"] + 1 if state["offset"] else 0,
                "timeout": 50,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            })
            data = r.json()
        except Exception:
            await asyncio.sleep(4)
            continue
        for u in data.get("result", []):
            state["offset"] = u["update_id"]
            save()
            asyncio.create_task(handle_update(u))
    print("پایان چرخه")

async def main():
    print(await api("getMe"))
    await engine.warm_all()
    await poll()
    await client.aclose()

try:
    asyncio.run(main())
except Exception:
    traceback.print_exc()
    raise SystemExit(1)
