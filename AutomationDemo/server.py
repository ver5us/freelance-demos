# -*- coding: utf-8 -*-
"""
AutomationDemo — демо автоматизации «форма записи → база → уведомление в Telegram».

Клиент выбирает дату и свободное время, как в боте: занятое время заблокировано,
двойная запись на один слот исключена. Каждая заявка получает номер.

Запуск: python server.py
Форма:            http://127.0.0.1:8080
Заявки (таблица): http://127.0.0.1:8080/leads
Выгрузка CSV:     http://127.0.0.1:8080/export.csv
"""
import csv
import html
import io
import os
import re
from datetime import date, datetime, timedelta

import aiohttp
import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_raw_admin = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID = int(_raw_admin) if _raw_admin.lstrip("-").isdigit() else None
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

DB_PATH = os.path.join(BASE_DIR, "leads.db")
HOST, PORT = "127.0.0.1", 8080

# Слоты записи: с 09:00 до 20:00 с шагом 1 час (последний слот 19:00–20:00)
TIME_SLOTS = [f"{hour:02d}:00" for hour in range(9, 20)]
DAYS_AHEAD = 7
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
SERVICES = ["Консультация", "Диагностика", "Процедура"]

STYLE = """
body{font-family:Segoe UI,Arial,sans-serif;background:#0e1626;color:#f0f4f8;
     margin:0;padding:40px 16px;display:flex;justify-content:center}
.card{background:#12233c;border-radius:14px;padding:32px 36px;max-width:520px;width:100%;
      box-shadow:0 10px 40px rgba(0,0,0,.4)}
h1{font-size:24px;margin:0 0 6px}
.sub{color:#9fb0c4;margin:0 0 24px;font-size:14px}
label{display:block;margin:14px 0 6px;font-size:14px;color:#c6d2e0}
input,select,textarea{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;
      border:1px solid #2a466b;background:#0b1220;color:#f0f4f8;font-size:15px}
textarea{resize:vertical;max-width:100%}
select option:disabled{color:#5b6b80}
.row{display:flex;gap:12px}
.row>div{flex:1}
button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:8px;cursor:pointer;
       background:#2aabee;color:#fff;font-size:16px;font-weight:600}
.err{color:#ff7b72;margin-top:12px}
.ok{color:#7ee2a8;font-size:15px}
.num{color:#2aabee;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid #2a466b;text-align:left}
th{color:#2aabee}
a{color:#2aabee}
"""


def human_date(date_iso: str) -> str:
    """'2026-10-20' -> 'Пн, 20.10'"""
    d = date.fromisoformat(date_iso)
    return f"{WEEKDAYS_RU[d.weekday()]}, {d.strftime('%d.%m')}"


def valid_dates() -> list:
    today = date.today()
    return [(today + timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD)]


def is_past_slot(date_iso: str, time_str: str) -> bool:
    if date_iso != date.today().isoformat():
        return False
    return int(time_str.split(":")[0]) <= datetime.now().hour


def form_html(error: str = "") -> str:
    """Форма записи: даты на 7 дней, слоты времени; занятые слоты блокируются скриптом."""
    dates_opts = "".join(
        f'<option value="{d}">{human_date(d)}</option>' for d in valid_dates()
    )
    time_opts = "".join(f'<option value="{t}">{t}</option>' for t in TIME_SLOTS)
    service_opts = "".join(f"<option>{s}</option>" for s in SERVICES)
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Запись — демо автоматизации</title><style>{STYLE}</style></head><body>
<div class="card">
<h1>Запись на консультацию</h1>
<p class="sub">Демо-форма: заявка упадёт в базу, на страницу /leads и мгновенно
в Telegram владельцу. Занятое время заблокировано, двойная запись исключена.</p>
{err}
<form method="post" action="/submit">
<label>Имя</label><input name="name" placeholder="Анна" required>
<label>Телефон</label><input name="phone" placeholder="+7 999 123-45-67" required>
<label>Услуга</label><select name="service">{service_opts}</select>
<div class="row"><div>
<label>Дата</label><select name="date" id="date">{dates_opts}</select>
</div><div>
<label>Время</label><select name="time" id="time">{time_opts}</select>
</div></div>
<label>Комментарий</label><textarea name="comment" rows="3" placeholder="Необязательно"></textarea>
<button type="submit">Отправить заявку</button>
</form>
<script>
const dateSel = document.getElementById('date');
const timeSel = document.getElementById('time');
async function refreshSlots() {{
  const resp = await fetch('/slots?date=' + dateSel.value);
  const busy = await resp.json();
  for (const opt of timeSel.options) {{
    opt.disabled = busy.includes(opt.value);
  }}
  if (timeSel.selectedIndex >= 0 && timeSel.options[timeSel.selectedIndex].disabled) {{
    timeSel.selectedIndex = -1;
  }}
}}
dateSel.addEventListener('change', refreshSlots);
refreshSlots();
</script>
</div></body></html>"""


def thanks_html(lead_id: int, service: str, date_iso: str, time_str: str) -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Заявка принята</title><style>{STYLE}</style></head><body>
<div class="card">
<h1 class="ok">Заявка принята</h1>
<p class="sub">Номер вашей записи: <span class="num">№{lead_id}</span><br>
{html.escape(service)}, {human_date(date_iso)} в {time_str}.<br>
Владелец уже получил уведомление в Telegram. Все заявки: <a href="/leads">/leads</a></p>
</div></body></html>"""


LEADS_HTML = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Заявки</title><style>{STYLE}</style></head><body>
<div class="card" style="max-width:1000px">
<h1>Заявки</h1>
<p class="sub">То, что владелец видит вместо потерянных сообщений. <a href="/export.csv">Скачать CSV</a></p>
<table><tr><th>№</th><th>Имя</th><th>Телефон</th><th>Услуга</th><th>Дата</th><th>Время</th>
<th>Комментарий</th><th>Когда создана</th></tr>
{{ROWS}}
</table></div></body></html>"""


def make_connector():
    """Прокси для Telegram API, если провайдер его блокирует."""
    if PROXY_URL:
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(PROXY_URL)
    return None


async def send_tg(text: str) -> None:
    """Уведомление владельцу: в Telegram или в консоль, если ADMIN_CHAT_ID не задан."""
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        print("\n===== УВЕДОМЛЕНИЕ ВЛАДЕЛЬЦУ =====\n" + text + "\n=================================\n")
        return
    connector = make_connector()
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_CHAT_ID, "text": text},
                # Короткий таймаут: форма не должна висеть, если Telegram недоступен
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    print("Telegram ответил ошибкой:", resp.status)
    except Exception as exc:  # не роняем сервер из-за сбоя уведомления
        print("Не удалось отправить уведомление:", exc)


async def init_db(app: web.Application) -> None:
    app["db"] = await aiosqlite.connect(DB_PATH)
    # Если база осталась от старой версии демо (без даты и времени) — пересоздаём.
    async with app["db"].execute("PRAGMA table_info(leads)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if columns and "date" not in columns:
        await app["db"].execute("DROP TABLE leads")
        columns = set()
    if not columns:
        await app["db"].execute(
            """
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                service TEXT NOT NULL,
                comment TEXT DEFAULT '',
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(date, time)
            )
            """
        )
    await app["db"].commit()


async def close_db(app: web.Application) -> None:
    await app["db"].close()


async def get_busy_times(db, date_iso: str) -> list:
    """Занятые слоты на дату: занятые записи плюс прошедшее время для сегодня."""
    async with db.execute(
        "SELECT time FROM leads WHERE date = ?", (date_iso,)
    ) as cursor:
        busy = {row[0] for row in await cursor.fetchall()}
    return sorted(t for t in TIME_SLOTS if t in busy or is_past_slot(date_iso, t))


async def page_form(request: web.Request) -> web.Response:
    return web.Response(text=form_html(), content_type="text/html")


async def page_slots(request: web.Request) -> web.Response:
    date_iso = request.query.get("date", "")
    if date_iso not in valid_dates():
        return web.json_response([])
    busy = await get_busy_times(request.app["db"], date_iso)
    return web.json_response(busy)


async def submit(request: web.Request) -> web.Response:
    data = await request.post()
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    service = (data.get("service") or "").strip()
    comment = (data.get("comment") or "").strip()
    date_iso = (data.get("date") or "").strip()
    time_str = (data.get("time") or "").strip()

    digits = re.sub(r"\D", "", phone)
    if len(name) < 2 or len(digits) < 10:
        return web.Response(
            text=form_html("Проверьте имя и телефон: нужно минимум 10 цифр."),
            content_type="text/html",
        )
    if date_iso not in valid_dates() or time_str not in TIME_SLOTS:
        return web.Response(
            text=form_html("Некорректные дата или время. Выберите из списка."),
            content_type="text/html",
        )
    db = request.app["db"]
    # Серверная проверка слота: форма могла быть открыта давно.
    if time_str in await get_busy_times(db, date_iso):
        return web.Response(
            text=form_html(
                f"Время {time_str} на {human_date(date_iso)} только что заняли. "
                "Выберите другой слот."
            ),
            content_type="text/html",
        )
    created_at = datetime.now().isoformat(timespec="seconds")
    try:
        cursor = await db.execute(
            """
            INSERT INTO leads (name, phone, service, comment, date, time, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (name, phone, service, comment, date_iso, time_str, created_at),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        # UNIQUE(date, time): гонка двух одновременных заявок на один слот
        return web.Response(
            text=form_html(
                f"Время {time_str} на {human_date(date_iso)} только что заняли. "
                "Выберите другой слот."
            ),
            content_type="text/html",
        )
    lead_id = cursor.lastrowid
    await send_tg(
        f"🔔 Новая запись №{lead_id}!\n\n"
        f"👤 Имя: {name}\n📞 Телефон: {phone}\n"
        f"💼 Услуга: {service}\n"
        f"📅 Когда: {human_date(date_iso)} в {time_str}\n"
        f"💬 Комментарий: {comment or '—'}"
    )
    return web.Response(
        text=thanks_html(lead_id, service, date_iso, time_str),
        content_type="text/html",
    )


async def page_leads(request: web.Request) -> web.Response:
    db = request.app["db"]
    async with db.execute(
        """
        SELECT id, name, phone, service, date, time, comment, created_at
        FROM leads ORDER BY id DESC
        """
    ) as cursor:
        rows = await cursor.fetchall()
    body = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
        "<td>{}</td><td>{}</td></tr>".format(
            r[0], html.escape(r[1]), html.escape(r[2]), html.escape(r[3]),
            human_date(r[4]), r[5], html.escape(r[6]), r[7],
        )
        for r in rows
    ) or "<tr><td colspan='8'>Заявок пока нет — отправьте первую с главной страницы.</td></tr>"
    return web.Response(text=LEADS_HTML.replace("{ROWS}", body), content_type="text/html")


async def export_csv(request: web.Request) -> web.Response:
    db = request.app["db"]
    async with db.execute(
        """
        SELECT id, name, phone, service, date, time, comment, created_at
        FROM leads ORDER BY id
        """
    ) as cursor:
        rows = await cursor.fetchall()
    buf = io.StringIO()
    # Разделитель ";" и BOM: русский Excel открывает файл по колонкам и без кракозябр
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        ["id", "name", "phone", "service", "date", "time", "comment", "created_at"]
    )
    writer.writerows(rows)
    return web.Response(
        text="﻿" + buf.getvalue(),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


app = web.Application()
app.on_startup.append(init_db)
app.on_cleanup.append(close_db)
app.router.add_get("/", page_form)
app.router.add_post("/submit", submit)
app.router.add_get("/slots", page_slots)
app.router.add_get("/leads", page_leads)
app.router.add_get("/export.csv", export_csv)

if __name__ == "__main__":
    print(f"Демо запущено: форма http://{HOST}:{PORT}, заявки http://{HOST}:{PORT}/leads")
    web.run_app(app, host=HOST, port=PORT, print=None)
