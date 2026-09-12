import os
import re
import json
from datetime import datetime
from icalendar import Calendar, Event
import pytz
from playwright.sync_api import sync_playwright

# ===== НАСТРОЙКИ =====
USERNAME = os.getenv("GUBKIN_LOGIN", "")
PASSWORD = os.getenv("GUBKIN_PASSWORD", "")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
SUBGROUP = 2
GROUP_ID = 9853
TIMEZONE = 'Europe/Moscow'
ICS_FILE = "schedule.ics"
USER_DATA_DIR = "browser_data"
SITE_URL = "https://lk.gubkin.ru/#/study/timetable"

TYPE_TAGS = {
    "Лекция": "[Л]",
    "Семинар": "[С]",
    "Лабораторная работа": "[ЛР]",
}

def get_schedule_json():
    print("🔄 Запускаю браузер...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()

        print("📡 Захожу на сайт расписания...")
        page.goto(SITE_URL, timeout=60000)
        page.wait_for_timeout(3000)

        pwd = page.locator("input[type='password']")
        if pwd.count() > 0 and pwd.first.is_visible():
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

            still_login = page.locator("input[type='password']")
            if still_login.count() > 0 and still_login.first.is_visible():
                if not HEADLESS:
                    print("🙏 Автовход не сработал. Войди вручную и нажми Enter...")
                    input()
                else:
                    raise Exception("Не удалось войти автоматически")
        else:
            print("✅ Сессия сохранена — вход не нужен!")

        print("📥 Перехватываю запрос сайта с расписанием...")
        with page.expect_response(
            lambda r: "act=schedule" in r.url and r.status == 200,
            timeout=30000
        ) as resp_info:
            page.reload(wait_until="networkidle")
        data = resp_info.value.json()
        context.close()
        return data

def parse_time_chunk(s):
    m = re.match(r'(\d+):(\d+)-(\d+):(\d+)', s)
    return tuple(map(int, m.groups())) if m else None

def create_ics(schedule_data):
    print("📅 Создаю ICS файл...")
    cal = Calendar()
    cal.add('prodid', '-//Gubkin Schedule//gubkin.ru//')
    cal.add('version', '2.0')
    tz = pytz.timezone(TIMEZONE)

    moscow = next((o for o in schedule_data['rows']['organizations'] if o['id'] == 0), None)
    if not moscow:
        print("❌ Не найдены данные по Москве")
        return

    chunks = moscow['lessonsTimeChunks']
    days = schedule_data['rows']['week']['weekRussia']['days']
    date_by_wd = {d['weekDayNumber']: d['date'] for d in days}

    count = 0
    for lesson in moscow['lessons']:
        if GROUP_ID not in [g['id'] for g in lesson.get('groups', [])]:
            continue
        if lesson.get('subgroup', 0) not in [0, SUBGROUP]:
            continue
        if lesson.get('isCanceled', False):
            continue

        date_str = date_by_wd.get(lesson['weekDayNumber'])
        if not date_str:
            continue
        lesson_date = datetime.strptime(date_str, "%d-%m-%Y").date()

        if lesson.get('isMoved') and lesson.get('movedTo'):
            try:
                lesson_date = datetime.strptime(lesson['movedTo'], "%d-%m-%Y").date()
            except Exception:
                pass

        tc = lesson['timeChunks']
        sh, sm, _, _ = parse_time_chunk(chunks[tc[0]])
        _, _, eh, em = parse_time_chunk(chunks[tc[-1]])
        start = tz.localize(datetime(lesson_date.year, lesson_date.month, lesson_date.day, sh, sm))
        end = tz.localize(datetime(lesson_date.year, lesson_date.month, lesson_date.day, eh, em))

        room = lesson['rooms'][0]['number'] if lesson.get('rooms') else '???'
        teachers = lesson.get('teachers', [])
        if teachers:
            t = teachers[0]
            pat = t['patronymic'][0] + '.' if t.get('patronymic') else ''
            teacher = f"{t['lastName']} {t['firstName'][0]}.{pat}"
        else:
            teacher = "преп. не указан"

        tag = TYPE_TAGS.get(lesson.get('type', ''), f"[{lesson.get('type', '')}]")

        event = Event()
        event.add('summary', f"{tag} {lesson['course']['name']}")
        event.add('dtstart', start)
        event.add('dtend', end)
        event.add('dtstamp', datetime.now(tz))
        event.add('description', teacher)
        event.add('location', f"Ауд. {room}")
        event.add('uid', f"{lesson['id']}_{lesson_date.strftime('%Y%m%d')}@gubkin.ru")
        cal.add_component(event)
        count += 1

    with open(ICS_FILE, 'wb') as f:
        f.write(cal.to_ical())
    print(f"✅ Создано {count} событий в файле {ICS_FILE}")

def main():
    print("🚀 Начинаю парсинг расписания Губкина...")
    try:
        data = get_schedule_json()
        if data and data.get('state'):
            create_ics(data)
            print("\n🎉 Готово! Файл schedule.ics создан.")
        else:
            print("❌ API вернул state=false.")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        raise

if __name__ == "__main__":
    main()