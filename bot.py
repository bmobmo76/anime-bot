import asyncio, json, os, pathlib, time
import httpx
import engine

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
RUN_SECONDS = 5.4 * 3600
START = time.time()

DATA = pathlib.Path("data")
DATA.mkdir(exist_ok=True)
STATE_F = DATA / "state.json"

state = {"offset": 0, "jobs": {}, "seq": 0}
if STATE_F.exists():
    try:
        state.update(json.loads(STATE_F.read_text()))
    except Exception:
        pass

def save():
    STATE_F.write_text(json.dumps(state))

def new_job(chat_id, payload):
    state["seq"] += 1
    jid = str(state["seq"])
    payload["chat_id"] = chat_id
    state["jobs"][jid] = payload
    save()
    return jid

client = httpx.AsyncClient(timeout=60)

async def api(method, files=None, **params):
    try:
        r = await client.post(f"{API}/{method}", data=params, files=files,
                              timeout=300)
        return r.json()
    except Exception as e:
        return {"ok": False, "desc": str(e)}

async def send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text}
    if kb:
        p["reply_markup"] = json.dumps({"inline_keyboard": kb})
    return await api("sendMessage", **p)

async def edit(chat_id, mid, text, kb=None):
    p = {"chat_id": chat_id, "message_id": mid, "text": text}
    if kb:
        p["reply_markup"] = json.dumps({"inline_keyboard": kb})
    return await api("editMessageText", **p)

async def send_video(chat_id, path, caption):
    with open(path, "rb") as f:
        files = {"video": (os.path.basename(path), f, "video/mp4")}
        return await api("sendVideo", files=files, chat_id=chat_id,
                         caption=caption, supports_streaming="true")

async def send_doc(chat_id, path, caption):
    with open(path, "rb") as f:
        files = {"document": (os.path.basename(path), f, "text/plain")}
        return await api("sendDocument", files=files, chat_id=chat_id,
                         caption=caption)


HELP = ("اسم انیمه را بفرست.\n"
        "سپس از دکمه‌ها سایت، نوع (زیرنویس/دوبله) و قسمت را انتخاب کن.\n"
        "فایل به‌صورت بخش‌های ~۴۵ مگابایتی ارسال می‌شود.")

async def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/start"):
        await send(chat_id, "سلام 👋\n" + HELP)
        return
    if text.startswith("/help"):
        await send(chat_id, HELP)
        return
    if text.startswith("/status"):
        up = int(time.time() - START)
        await send(chat_id, f"فعال. زمان اجرا: {up // 60} دقیقه.\n"
                            f"کارهای در انتظار: {len(state['jobs'])}")
        return
    if text.startswith("/"):
        return

    await send(chat_id, f"🔎 جستجوی «{text}» در ۵ سایت...")
    results = await engine.search_all(text)
    if not results:
        await send(chat_id, "چیزی پیدا نشد. املای اسم را عوض کن یا نام انگلیسی بفرست.")
        return

    jid = new_job(chat_id, {"type": "anime", "results": results})
    kb = []
    for i, r in enumerate(results[:10]):
        site = engine.SITES[r["site"]].name
        kb.append([{"text": f"[{site}] {r['title'][:60]}",
                    "callback_data": f"a|{jid}|{i}"}])
    await send(chat_id, "کدام را می‌خواهی؟", kb)


async def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    await api("answerCallbackQuery", callback_query_id=cb["id"])
    parts = cb["data"].split("|")
    action = parts[0]

    if action == "a":
        jid, idx = parts[1], int(parts[2])
        job = state["jobs"].get(jid)
        if not job:
            return await edit(chat_id, mid, "منقضی شد، دوباره جستجو کن.")
        chosen = job["results"][idx]
        job.update(site=chosen["site"], anime_url=chosen["url"], title=chosen["title"])
        save()
        kb = [[{"text": "زیرنویس انگلیسی", "callback_data": f"d|{jid}|sub"}],
              [{"text": "دوبله انگلیسی", "callback_data": f"d|{jid}|dub"}]]
        await edit(chat_id, mid, f"{chosen['title']}\nکدام نسخه؟", kb)

    elif action == "d":
        jid, kind = parts[1], parts[2]
        job = state["jobs"].get(jid)
        if not job:
            return await edit(chat_id, mid, "منقضی شد.")
        await edit(chat_id, mid, "⏳ گرفتن لیست قسمت‌ها...")
        try:
            eps = await asyncio.to_thread(
                engine.SITES[job["site"]].episodes, job["anime_url"])
        except Exception as e:
            return await edit(chat_id, mid, f"لیست قسمت‌ها نیامد: {e}")
        if not eps:
            return await edit(chat_id, mid, "قسمتی پیدا نشد.")
        job.update(kind=kind, episodes=eps)
        save()
        await show_page(chat_id, mid, jid, 0)

    elif action == "p":
        await show_page(chat_id, mid, parts[1], int(parts[2]))

    elif action == "e":
        jid, num = parts[1], parts[2]
        job = state["jobs"].get(jid)
        if not job:
            return await edit(chat_id, mid, "منقضی شد.")
        kb = [[{"text": "۴۸۰p (سبک‌تر)", "callback_data": f"q|{jid}|{num}|480"},
               {"text": "۷۲۰p", "callback_data": f"q|{jid}|{num}|720"}]]
        await edit(chat_id, mid, f"قسمت {num} — کیفیت؟", kb)

    elif action == "q":
        jid, num, height = parts[1], parts[2], int(parts[3])
        job = state["jobs"].get(jid)
        if not job:
            return await edit(chat_id, mid, "منقضی شد.")
        await edit(chat_id, mid, f"⬇️ قسمت {num} در حال دانلود...")
        asyncio.create_task(run_job(job, num, height))


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
        nav.append({"text": "◀️", "callback_data": f"p|{jid}|{page - 1}"})
    if (page + 1) * per < len(eps):
        nav.append({"text": "▶️", "callback_data": f"p|{jid}|{page + 1}"})
    if nav:
        rows.append(nav)
    await edit(chat_id, mid,
               f"{job['title']} — {job['kind']}\nقسمت را انتخاب کن:", rows)


sem = asyncio.Semaphore(2)

async def run_job(job, num, height):
    async with sem:
        chat_id = job["chat_id"]
        ep = next((e for e in job["episodes"] if e["num"] == num), None)
        if not ep:
            await send(chat_id, "قسمت پیدا نشد.")
            return
        try:
            url = await engine.resolve(job["site"], ep, job["kind"])
        except Exception as e:
            url = None
            await send(chat_id, f"خطا در استخراج لینک: {e}")
        if not url:
            await send(chat_id, "لینک این قسمت درنیامد (احتمالاً سرور دوبله ندارد "
                                "یا ساختار سایت عوض شده).")
            return

        loop = asyncio.get_running_loop()
        try:
            video, subs = await loop.run_in_executor(
                None, engine.download, url, ep["url"], job["kind"], height)
        except Exception as e:
            await send(chat_id, f"❌ دانلود نشد:\n{str(e)[:300]}")
            return

        try:
            parts = await loop.run_in_executor(None, engine.split, video)
        except Exception as e:
            await send(chat_id, f"❌ تقسیم فایل نشد: {e}")
            return

        for i, p in enumerate(parts):
            cap = f"{job['title']} — قسمت {num}"
            if len(parts) > 1:
                cap += f" (بخش {i + 1}/{len(parts)})"
            ok = await send_video(chat_id, p, cap)
            if not ok.get("ok"):
                await send(chat_id, f"ارسال بخش {i + 1} نشد: {ok.get('desc')}")
            await asyncio.sleep(1)

        if subs:
            await send_doc(chat_id, subs, "زیرنویس انگلیسی")

        engine.cleanup(video, subs, *parts)
        state["jobs"].pop(next((k for k, v in state["jobs"].items() if v is job), ""), None)
        save()


async def handle_update(u):
    try:
        if "message" in u:
            await handle_message(u["message"])
        elif "callback_query" in u:
            await handle_callback(u["callback_query"])
    except Exception as e:
        print("handler error:", e)


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
    print("پایان چرخه، خروج تمیز")


async def main():
    await send_ok = await api("getMe")
    print(send_ok)
    await poll()
    await client.aclose()

asyncio.run(main())
