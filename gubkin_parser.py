import os
import re
import json
import base64
from datetime import datetime, timedelta
from icalendar import Calendar, Event
import pytz
import requests
from playwright.sync_api import sync_playwright

# ===== НАСТРОЙКИ =====
USERNAME = os.getenv("GUBKIN_LOGIN", "")
PASSWORD = os.getenv("GUBKIN_PASSWORD", "")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
SUBGROUP = 2
GROUP_ID = 9853
TIMEZONE = 'Europe/Moscow'
ICS_FILE = "schedule.ics"
HISTORY_FILE = "events_history.json"
USER_DATA_DIR = "browser_data"
SITE_URL = "https://lk.gubkin.ru/#/study/timetable"

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = "Gequuy"     # ← не забудь вписать
GITHUB_REPO = "gub-schedule"  # ← имя репозитория

TYPE_TAGS = {
    "Лекция": "[Л]",
    "Семинар": "[С]",
    "Лабораторная работа": "[ЛР]",
}

# ===== БРАУЗЕР И ЗАБОР ДАННЫХ =====

def open_site(page):
    last_err = None
    for attempt in range(3):
        try:
            print(f"📡 Попытка {attempt + 1}: захожу на сайт...")
            page.goto(SITE_URL, timeout=90000, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            return
        except Exception as e:
            last_err = e
            print(f"⚠️ Попытка {attempt + 1} не удалась: {e}")
            page.wait_for_timeout(5000)
    raise last_err

def login_if_needed(page):
    pwd = page.locator("input[type='password']")
    if not (pwd.count() > 0 and pwd.first.is_visible()):
        print("✅ Сессия сохранена — вход не нужен!")
        return
    if not USERNAME or not PASSWORD:
        raise Exception("Не заданы GUBKIN_LOGIN / GUBKIN_PASSWORD")
    print("🔑 Вхожу автоматически...")
    page.locator("input:visible:not([type='password']):not([type='checkbox']):not([type='hidden'])").first.fill(USERNAME)
    pwd.first.fill(PASSWORD)
    btn = page.locator('button[type="submit"]')
    if btn.count() == 0:
        btn = page.locator("button:visible")
    btn.first.click()
    page.wait_for_timeout(5000)
    still = page.locator("input[type='password']")
    if still.count() > 0 and still.first.is_visible():
        if not HEADLESS:
            print("🙏 Автовход не сработал. Войди вручную и нажми Enter...")
            input()
        else:
            raise Exception("Не удалось войти автоматически")

def fetch_week_by_url(page, url, headers):
    safe = {k: v for k, v in headers.items()
            if k.lower() not in ("host", "content-length", "connection", "accept-encoding", "cookie")}
    res = page.evaluate(
        """async ([url, headers]) => {
            const r = await fetch(url, { headers, credentials: 'include' });
            return { status: r.status, text: await r.text() };
        }""",
        [url, safe],
    )
    if res["status"] != 200 or not res["text"].strip():
        return None
    try:
        return json.loads(res["text"])
    except Exception:
        return None

def next_week_url(url):
    m = re.search(r"date=(\d{1,2})-(\d{1,2})-(\d{4})", url)
    if not m:
        return None
    padded = len(m.group(1)) == 2
    monday = datetime.now() - timedelta(days=datetime.now().weekday()) + timedelta(days=7)
    ds = monday.strftime("%d-%m-%Y") if padded else f"{monday.day}-{monday.month}-{monday.year}"
    return re.sub(r"date=\d{1,2}-\d{1,2}-\d{4}", f"date={ds}", url)

def collect_weeks():
    print("🔄 Запускаю браузер...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=HEADLESS, viewport={"width": 1280, "height": 800})
        page = context.pages[0] if context.pages else context.new_page()
        open_site(page)
        login_if_needed(page)

        print("📥 Перехватываю запрос текущей недели...")
        with page.expect_response(
            lambda r: "act=schedule" in r.url and r.status == 200, timeout=60000
        ) as ri:
            page.reload(wait_until="domcontentloaded")
        resp = ri.value
        data_current = resp.json()
        url, headers = resp.url, resp.request.headers

        data_next = None
        url2 = next_week_url(url)
        if url2:
            print("📥 Забираю следующую неделю...")
            data_next = fetch_week_by_url(page, url2, headers)
            if data_next is None:
                print("⚠️ Следующая неделя не получена (ещё не опубликована?)")
        context.close()
        return data_current, data_next

# ===== РАЗБОР НЕДЕЛИ =====

def parse_time_chunk(s):
    m = re.match(r"(\d+):(\d+)-(\d+):(\d+)", s)
    return tuple(map(int, m.groups())) if m else None

def week_to_events(data):
    events, cancelled, moved = {}, set(), []
    moscow = next((o for o in data["rows"]["organizations"] if o["id"] == 0), None)
    if not moscow:
        return events, cancelled, moved
    chunks = moscow["lessonsTimeChunks"]
    days = data["rows"]["week"]["weekRussia"]["days"]
    date_by_wd = {d["weekDayNumber"]: d["date"] for d in days}

    for lesson in moscow["lessons"]:
        if GROUP_ID not in [g["id"] for g in lesson.get("groups", [])]:
            continue
        if lesson.get("subgroup", 0) not in (0, SUBGROUP):
            continue
        # Пропускаем консультации (они приходят как тип "Мероприятие" с названием "Консультация")
        name_low = lesson.get("course", {}).get("name", "").lower()
        type_low = lesson.get("type", "").lower()
        if "консульт" in name_low or "консульт" in type_low:
            continue
        ds = date_by_wd.get(lesson["weekDayNumber"])
        if not ds:
            continue
        lesson_date = datetime.strptime(ds, "%d-%m-%Y").date()
        if lesson.get("isMoved") and lesson.get("movedTo"):
            try:
                lesson_date = datetime.strptime(lesson["movedTo"], "%d-%m-%Y").date()
            except Exception:
                pass
        uid = f"{lesson['id']}_{lesson_date.strftime('%Y%m%d')}@gubkin.ru"
        if lesson.get("isCanceled", False):
            cancelled.add(uid)
            continue
        tc = lesson["timeChunks"]
        sh, sm, _, _ = parse_time_chunk(chunks[tc[0]])
        _, _, eh, em = parse_time_chunk(chunks[tc[-1]])
        start = datetime(lesson_date.year, lesson_date.month, lesson_date.day, sh, sm)
        end = datetime(lesson_date.year, lesson_date.month, lesson_date.day, eh, em)
        room = lesson["rooms"][0]["number"] if lesson.get("rooms") else "???"
        teachers = lesson.get("teachers", [])
        if teachers:
            t = teachers[0]
            pat = t["patronymic"][0] + "." if t.get("patronymic") else ""
            teacher = f"{t['lastName']} {t['firstName'][0]}.{pat}"
        else:
            teacher = "преп. не указан"
        tag = TYPE_TAGS.get(lesson.get("type", ""), f"[{lesson.get('type', '')}]")
        events[uid] = {
            "summary": f"{tag} {lesson['course']['name']}",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "description": teacher,
            "location": f"Ауд. {room}",
        }
        if lesson.get("isMoved") and lesson.get("movedTo"):
            moved.append((lesson["id"], uid))
    return events, cancelled, moved

# ===== ИСТОРИЯ СОБЫТИЙ =====

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)

def seed_from_github(history):
    """Если локальной истории нет — восстанавливаем её из опубликованного ICS"""
    url = f"https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}/{ICS_FILE}"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return
        cal = Calendar.from_ical(r.content)
        n = 0
        for comp in cal.walk("VEVENT"):
            uid = str(comp.get("uid"))
            if uid in history:
                continue
            history[uid] = {
                "summary": str(comp.get("summary")),
                "start": comp.get("dtstart").dt.replace(tzinfo=None).isoformat(),
                "end": comp.get("dtend").dt.replace(tzinfo=None).isoformat(),
                "description": str(comp.get("description") or ""),
                "location": str(comp.get("location") or ""),
            }
            n += 1
        if n:
            print(f"📚 Восстановлено {n} прошлых событий из GitHub")
    except Exception as e:
        print(f"⚠️ Не удалось восстановить историю из GitHub: {e}")

# ===== СБОРКА И ЗАГРУЗКА =====

def build_ics(history):
    cal = Calendar()
    cal.add("prodid", "-//Gubkin Schedule//gubkin.ru//")
    cal.add("version", "2.0")
    tz = pytz.timezone(TIMEZONE)
    for uid, rec in sorted(history.items(), key=lambda kv: kv[1]["start"]):
        event = Event()
        event.add("summary", rec["summary"])
        event.add("dtstart", tz.localize(datetime.fromisoformat(rec["start"])))
        event.add("dtend", tz.localize(datetime.fromisoformat(rec["end"])))
        event.add("dtstamp", datetime.now(tz))
        if rec.get("description"):
            event.add("description", rec["description"])
        if rec.get("location"):
            event.add("location", rec["location"])
        event.add("uid", uid)
        cal.add_component(event)
    with open(ICS_FILE, "wb") as f:
        f.write(cal.to_ical())
    print(f"✅ Собрано {len(history)} событий (прошлые + текущая + следующая неделя)")

def upload_to_github():
    if not GITHUB_TOKEN:
        print("ℹ️ GITHUB_TOKEN не задан — пропускаю загрузку")
        return
    print("☁️ Загружаю файл на GitHub...")
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{ICS_FILE}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    with open(ICS_FILE, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    sha = None
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        sha = r.json().get("sha")
    data = {"message": "Auto-update schedule", "content": content_b64}
    if sha:
        data["sha"] = sha
    r2 = requests.put(url, headers=headers, json=data)
    if r2.status_code in (200, 201):
        print("✅ Файл обновлён на GitHub!")
    else:
        print(f"❌ Ошибка загрузки: {r2.status_code} {r2.text[:200]}")

def main():
    print("🚀 Начинаю парсинг расписания Губкина...")
    data_current, data_next = collect_weeks()

    history = load_history()
    if not history:
        seed_from_github(history)

    fetched_dates = set()
    all_events, all_cancelled, all_moved = {}, set(), []
    for data in (data_current, data_next):
        if not data or not data.get("state"):
            continue
        for d in data["rows"]["week"]["weekRussia"]["days"]:
            fetched_dates.add(datetime.strptime(d["date"], "%d-%m-%Y").strftime("%Y%m%d"))
        ev, canc, mv = week_to_events(data)
        all_events.update(ev)
        all_cancelled |= canc
        all_moved.extend(mv)

    # Отменённые пары убираем из календаря
    for uid in all_cancelled:
        history.pop(uid, None)
    # Перенесённые: чистим старые слоты, но ТОЛЬКО в пределах загруженных недель
    for base, keep_uid in all_moved:
        for key in list(history):
            if key.startswith(f"{base}_") and key != keep_uid:
                datepart = key.split("_")[1].split("@")[0]
                if datepart in fetched_dates:
                    del history[key]

    # Убираем консультации из истории (включая прошлые)
    for key in list(history):
        if "консульт" in history[key].get("summary", "").lower():
            del history[key]

    history.update(all_events)
    save_history(history)
    build_ics(history)
    upload_to_github()
    print("\n🎉 Готово!")

if __name__ == "__main__":
    main()
