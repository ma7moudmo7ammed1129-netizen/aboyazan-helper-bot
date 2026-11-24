import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
TZ = pytz.timezone("Africa/Cairo")
LAT = 31.10056
LON = 30.31278
METHOD = 5
DATA_FILE = Path("data.json")
PRAYERS = [
    ("Fajr", "الفجر"),
    ("Dhuhr", "الظهر"),
    ("Asr", "العصر"),
    ("Maghrib", "المغرب"),
    ("Isha", "العشاء"),
]

scheduler = BackgroundScheduler(timezone=TZ)
app = None


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"chat_id": None, "tasks": []}


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_prayers(day):
    url = "https://api.aladhan.com/v1/timings/" + day.strftime("%d-%m-%Y")
    r = requests.get(
        url,
        params={"latitude": LAT, "longitude": LON, "method": METHOD},
        timeout=20,
    )
    r.raise_for_status()
    timings = r.json()["data"]["timings"]
    out = []
    for key, name in PRAYERS:
        hh, mm = timings[key].split(":")[:2]
        when = TZ.localize(datetime(day.year, day.month, day.day, int(hh), int(mm)))
        out.append((key, name, when))
    return out


def send_text(chat_id, text):
    if not chat_id:
        return
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )


def remind_prayer(name):
    data = load_data()
    send_text(data.get("chat_id"), f"تنبيه: باقي 10 دقايق على أذان {name}")


def remind_task(task_id):
    data = load_data()
    task = next((t for t in data["tasks"] if t["id"] == task_id), None)
    if not task or task.get("done"):
        return
    send_text(data.get("chat_id"), f"تذكير: {task['text']}")
    task["done"] = True
    save_data(data)


def schedule_prayers():
    now = datetime.now(TZ)
    for offset in (0, 1):
        day = (now + timedelta(days=offset)).date()
        try:
            prayers = fetch_prayers(datetime.combine(day, datetime.min.time()))
        except Exception as exc:
            print("prayer fetch failed", exc)
            continue
        for _key, name, when in prayers:
            alert = when - timedelta(minutes=10)
            if alert <= now:
                continue
            job_id = f"prayer-{alert.isoformat()}"
            if scheduler.get_job(job_id):
                continue
            scheduler.add_job(
                remind_prayer,
                "date",
                run_date=alert,
                args=[name],
                id=job_id,
                replace_existing=True,
            )


def schedule_saved_tasks():
    now = datetime.now(TZ)
    data = load_data()
    for task in data["tasks"]:
        if task.get("done"):
            continue
        when = datetime.fromisoformat(task["when"])
        if when.tzinfo is None:
            when = TZ.localize(when)
        if when <= now:
            continue
        job_id = f"task-{task['id']}"
        if scheduler.get_job(job_id):
            continue
        scheduler.add_job(
            remind_task,
            "date",
            run_date=when,
            args=[task["id"]],
            id=job_id,
            replace_existing=True,
        )


def next_id(data):
    return max((t["id"] for t in data["tasks"]), default=0) + 1


AR_HOURS = {
    "واحد": 1, "واحدة": 1, "اتنين": 2, "اثنين": 2, "تنين": 2,
    "تلاتة": 3, "ثلاثة": 3, "اربعة": 4, "أربعة": 4,
    "خمسة": 5, "ستة": 6, "سبعة": 7, "تمانية": 8, "ثمانية": 8,
    "تسعة": 9, "عشرة": 10, "حداشر": 11, "اتناشر": 12, "اثناشر": 12,
}


def parse_when(text, now):
    cleaned = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه")
    m = re.search(r"(\d{1,2})[:\.](\d{2})", cleaned)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?:الساعه|الساعة|ساعه|ساعة)?\s*(\d{1,2}|واحده|واحد|اتنين|اثنين|تنين|تلاته|ثلاثه|اربعه|خمسه|سته|سبعه|تمانيه|ثمانيه|تسعه|عشره|حداشر|اتناشر|اثناشر)", cleaned)
        if not m:
            return None
        raw = m.group(1)
        hour = int(raw) if raw.isdigit() else AR_HOURS.get(raw)
        if hour is None:
            return None
        minute = 30 if "ونص" in cleaned or "و نص" in cleaned else 0
        if "ربع" in cleaned and "ونص" not in cleaned:
            minute = 15
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if hour <= 12:
        if any(w in cleaned for w in ("مساء", "بالليل", "الليل", "العصر", "المغرب", "العشا", "العشاء", "الظهر")):
            if hour < 12:
                hour += 12
        elif any(w in cleaned for w in ("الصبح", "صباحا", "صباح", "الفجر")):
            if hour == 12:
                hour = 0
        elif hour < 7:
            hour += 12
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if any(w in cleaned for w in ("بكره", "بكرة", "غدا", "غداً")) or when <= now:
        when += timedelta(days=1)
    return when


def task_body(text):
    body = re.sub(r"^ذكرني\s*", "", text.strip())
    body = re.sub(r"(بكره|بكرة|غداً|غدا)", "", body)
    body = re.sub(r"(الساعه|الساعة|ساعه|ساعة)", "", body)
    body = re.sub(r"\d{1,2}[:\.]\d{2}", "", body)
    body = re.sub(r"\d{1,2}", "", body, count=1)
    for word in ("ونص", "و نص", "وربع", "الصبح", "صباحا", "مساء", "بالليل", "الفجر", "الظهر", "العصر", "المغرب", "العشاء", "العشا"):
        body = body.replace(word, "")
    body = " ".join(body.split())
    return body or "تذكير"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data["chat_id"] = update.effective_chat.id
    save_data(data)
    schedule_prayers()
    await update.message.reply_text(
        "أهلاً بيك.\n"
        "هفكرك قبل كل أذان بـ 10 دقايق (بلقطر الشرقية - أبو حمص).\n\n"
        "الأوامر:\n"
        "اكتب: مواقيت\n"
        "اكتب: مهام\n"
        "أو /prayers و /tasks\n\n"
        "مثال:\n"
        "ذكرني الساعة 5 العصر أروح السوق\n"
        "ذكرني بكرة 8 الصبح أذاكر"
    )


async def prayers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data["chat_id"] = update.effective_chat.id
    save_data(data)
    now = datetime.now(TZ)
    lines = ["مواقيت النهاردة:"]
    for _key, name, when in fetch_prayers(now):
        lines.append(f"{name}: {when.strftime('%I:%M %p')}  (تنبيه { (when - timedelta(minutes=10)).strftime('%I:%M %p') })")
    await update.message.reply_text("\n".join(lines))


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    pending = [t for t in data["tasks"] if not t.get("done")]
    if not pending:
        await update.message.reply_text("مفيش تذكيرات محفوظة.")
        return
    lines = ["تذكيراتك:"]
    for t in pending:
        when = datetime.fromisoformat(t["when"])
        lines.append(f"- {when.strftime('%m/%d %I:%M %p')}  {t['text']}")
    await update.message.reply_text("\n".join(lines))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text in ("مواقيت", "/مواقيت", "الصلاة", "الاذان", "الأذان"):
        await prayers_cmd(update, context)
        return
    if text in ("مهام", "/مهام", "تذكيرات", "مهامي"):
        await tasks_cmd(update, context)
        return
    if not text.startswith("ذكرني"):
        await update.message.reply_text("اكتب التذكير كده: ذكرني الساعة 5 العصر أروح السوق")
        return
    now = datetime.now(TZ)
    when = parse_when(text, now)
    if when is None:
        await update.message.reply_text("مش فاهم الساعة. جرب: ذكرني الساعة 5 العصر أروح السوق")
        return
    data = load_data()
    data["chat_id"] = update.effective_chat.id
    task = {"id": next_id(data), "text": task_body(text), "when": when.isoformat(), "done": False}
    data["tasks"].append(task)
    save_data(data)
    scheduler.add_job(
        remind_task,
        "date",
        run_date=when,
        args=[task["id"]],
        id=f"task-{task['id']}",
        replace_existing=True,
    )
    await update.message.reply_text(
        f"تمام، هفكرك يوم {when.strftime('%m/%d')} الساعة {when.strftime('%I:%M %p')}: {task['text']}"
    )


def main():
    global app
    scheduler.start()
    schedule_prayers()
    schedule_saved_tasks()
    scheduler.add_job(schedule_prayers, "cron", hour=0, minute=5, id="daily-prayers", replace_existing=True)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("prayers", prayers_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(MessageHandler(filters.TEXT & \~filters.COMMAND, on_text))
    print("bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
