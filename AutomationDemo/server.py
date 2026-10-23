# -*- coding: utf-8 -*-
"""
AutomationDemo — демо автоматизации «форма записи → база → уведомление в Telegram».

Клиент выбирает услугу, дату и свободное время, как в боте записи:
слот занят под конкретную услугу, двойная запись исключена, каждая заявка
получает номер. Клиент может отменить свою запись по номеру телефона.
Каталог услуг, даты и слоты — те же, что у BotRecord (единая логика записи).

Запуск: python server.py
Форма:            http://127.0.0.1:8080
Заявки (таблица): http://127.0.0.1:8080/leads
Выгрузки:         http://127.0.0.1:8080/export.csv и /export.xlsx
Отмена клиентом:  http://127.0.0.1:8080/cancel
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

# Каталог услуг и слоты — те же, что у бота BotRecord: демо показывает
# единую логику записи для сайта и для Telegram.
SERVICES = {
    1: {"name": "Стрижка", "price": 1500},
    2: {"name": "Маникюр", "price": 2000},
    3: {"name": "Консультация специалиста", "price": 1000},
    4: {"name": "Уходовая процедура", "price": 2500},
}
TIME_SLOTS = [f"{hour:02d}:00" for hour in range(9, 20)]
DAYS_AHEAD = 7
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

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
button.small{width:auto;margin:4px 4px 4px 0;padding:8px 14px;font-size:14px;
       background:transparent;border:1px solid #ff7b72;color:#ff7b72}
.err{color:#ff7b72;margin-top:12px}
.ok{color:#7ee2a8;font-size:15px}
.num{color:#2aabee;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid #2a466b;text-align:left}
th{color:#2aabee}
.cancelled{color:#8494a8;text-decoration:line-through}
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


def phone_digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def validate_phone(phone: str) -> bool:
    """Телефон: от 10 до 11 цифр, не больше."""
    return 10 <= len(phone_digits(phone)) <= 11


def form_html(error: str = "", values: dict = None) -> str:
    """Форма записи. При ошибке введённые значения сохраняются."""
    values = values or {}
    dates_opts = "".join(
        f'<option value="{d}"{" selected" if values.get("date") == d else ""}>'
        f"{human_date(d)}</option>"
        for d in valid_dates()
    )
    time_opts = "".join(
        f'<option value="{t}"{" selected" if values.get("time") == t else ""}>'
        f"{t}</option>"
        for t in TIME_SLOTS
    )
    service_opts = "".join(
        f'<option value="{sid}"{" selected" if str(values.get("service")) == str(sid) else ""}>'
        f'{s["name"]} — {s["price"]} ₽</option>'
        for sid, s in SERVICES.items()
    )
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    name_v = html.escape(values.get("name", ""))
    phone_v = html.escape(values.get("phone", ""))
    comment_v = html.escape(values.get("comment", ""))
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Запись — демо автоматизации</title><style>{STYLE}</style></head><body>
<div class="card">
<h1>Онлайн-запись</h1>
<p class="sub">Демо-форма: заявка упадёт в базу, на страницу владельца и мгновенно
в Telegram. Занятое время заблокировано под каждую услугу отдельно.</p>
{err}
<form method="post" action="/submit">
<label>Имя</label><input name="name" placeholder="Анна" value="{name_v}" required>
<label>Телефон</label><input name="phone" placeholder="+7 999 123-45-67" value="{phone_v}" required>
<label>Услуга</label><select name="service" id="service">{service_opts}</select>
<div class="row"><div>
<label>Дата</label><select name="date" id="date">{dates_opts}</select>
</div><div>
<label>Время</label><select name="time" id="time">{time_opts}</select>
</div></div>
<label>Комментарий</label><textarea name="comment" rows="3"
placeholder="Необязательно">{comment_v}</textarea>
<button type="submit">Отправить заявку</button>
</form>
<script>
const serviceSel = document.getElementById('service');
const dateSel = document.getElementById('date');
const timeSel = document.getElementById('time');
async function refreshSlots() {{
  const resp = await fetch('/slots?date=' + dateSel.value + '&service=' + serviceSel.value);
  const busy = await resp.json();
  const current = timeSel.value;
  for (const opt of timeSel.options) {{
    opt.disabled = busy.includes(opt.value);
  }}
  if (current && !busy.includes(current)) {{ timeSel.value = current; }}
  else if (timeSel.options[timeSel.selectedIndex] &&
           timeSel.options[timeSel.selectedIndex].disabled) {{
    timeSel.selectedIndex = -1;
  }}
}}
serviceSel.addEventListener('change', refreshSlots);
dateSel.addEventListener('change', refreshSlots);
refreshSlots();
</script>
</div></body></html>"""


def thanks_html(lead_id: int, service: str, date_iso: str, time_str: str) -> str:
    """Страница для клиента: номер записи и слот. Служебных ссылок нет."""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Заявка принята</title><style>{STYLE}</style></head><body>
<div class="card">
<h1 class="ok">Заявка принята</h1>
<p class="sub">Номер вашей записи: <span class="num">№{lead_id}</span><br>
{html.escape(service)}, {human_date(date_iso)} в {time_str}.<br>
Мы получили уведомление и ждём вас. Если планы изменятся —
отмените запись на <a href="/cancel">странице отмены</a> по номеру телефона.</p>
</div></body></html>"""


CANCEL_FORM_HTML = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Отмена записи</title><style>{STYLE}</style></head><body>
<div class="card">
<h1>Отмена записи</h1>
<p class="sub">Введите телефон, на который оформляли запись, — покажем ваши
активные записи, и вы отмените нужную.</p>
<form method="post" action="/cancel">
<label>Телефон</label><input name="phone" placeholder="+7 999 123-45-67" required>
<button type="submit">Показать мои записи</button>
</form>
</div></body></html>"""


LEADS_HTML = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Заявки</title><style>{STYLE}</style></head><body>
<div class="card" style="max-width:1100px">
<h1>Заявки</h1>
<p class="sub">То, что владелец видит вместо потерянных сообщений.
<a href="/export.csv">Скачать CSV</a> · <a href="/export.xlsx">Скачать XLSX</a></p>
<table><tr><th>№</th><th>Имя</th><th>Телефон</th><th>Услуга</th><th>Дата</th><th>Время</th>
<th>Комментарий</th><th>Статус</th><th>Когда создана</th></tr>
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
    # Старую схему демо-базы (без статуса и привязки слота к услуге) пересоздаём.
    async with app["db"].execute("PRAGMA table_info(leads)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if columns and "status" not in columns:
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
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )
            """
        )
        # Слот занят в разрезе «услуга + дата + время» среди активных записей.
        await app["db"].execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_active
                ON leads(service, date, time) WHERE status = 'active'
            """
        )
    await app["db"].commit()


async def close_db(app: web.Application) -> None:
    await app["db"].close()


async def get_busy_times(db, date_iso: str, service: str) -> list:
    """Занятые слоты на дату для конкретной услуги + прошедшее время сегодня."""
    async with db.execute(
        "SELECT time FROM leads WHERE date = ? AND service = ? AND status = 'active'",
        (date_iso, service),
    ) as cursor:
        busy = {row[0] for row in await cursor.fetchall()}
    return sorted(t for t in TIME_SLOTS if t in busy or is_past_slot(date_iso, t))


async def page_form(request: web.Request) -> web.Response:
    return web.Response(text=form_html(), content_type="text/html")


async def page_slots(request: web.Request) -> web.Response:
    date_iso = request.query.get("date", "")
    service_id = request.query.get("service", "")
    service = SERVICES.get(int(service_id), {}).get("name") if service_id.isdigit() else None
    if date_iso not in valid_dates() or service is None:
        return web.json_response([])
    busy = await get_busy_times(request.app["db"], date_iso, service)
    return web.json_response(busy)


async def submit(request: web.Request) -> web.Response:
    data = await request.post()
    values = {
        "name": (data.get("name") or "").strip(),
        "phone": (data.get("phone") or "").strip(),
        "service": (data.get("service") or "").strip(),
        "comment": (data.get("comment") or "").strip(),
        "date": (data.get("date") or "").strip(),
        "time": (data.get("time") or "").strip(),
    }

    def fail(msg: str) -> web.Response:
        return web.Response(text=form_html(msg, values), content_type="text/html")

    if len(values["name"]) < 2:
        return fail("Проверьте имя: минимум 2 символа.")
    if not validate_phone(values["phone"]):
        return fail("Проверьте телефон: от 10 до 11 цифр, например +7 999 123-45-67.")
    service = SERVICES.get(int(values["service"]), {}).get("name") \
        if values["service"].isdigit() else None
    if service is None:
        return fail("Выберите услугу из списка.")
    if values["date"] not in valid_dates() or values["time"] not in TIME_SLOTS:
        return fail("Некорректные дата или время. Выберите из списка.")

    db = request.app["db"]
    # Серверная проверка слота под услугу: форма могла быть открыта давно.
    if values["time"] in await get_busy_times(db, values["date"], service):
        return fail(
            f"Время {values['time']} на {human_date(values['date'])} под услугу "
            f"«{service}» только что заняли. Выберите другой слот."
        )
    created_at = datetime.now().isoformat(timespec="seconds")
    try:
        cursor = await db.execute(
            """
            INSERT INTO leads (name, phone, service, comment, date, time, status, created_at)
            VALUES (?,?,?,?,?,?, 'active', ?)
            """,
            (values["name"], values["phone"], service, values["comment"],
             values["date"], values["time"], created_at),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        # UNIQUE(service, date, time) среди активных: гонка двух заявок на слот
        return fail(
            f"Время {values['time']} на {human_date(values['date'])} под услугу "
            f"«{service}» только что заняли. Выберите другой слот."
        )
    lead_id = cursor.lastrowid
    await send_tg(
        f"🔔 Новая запись №{lead_id}!\n\n"
        f"👤 Имя: {values['name']}\n📞 Телефон: {values['phone']}\n"
        f"💼 Услуга: {service}\n"
        f"📅 Когда: {human_date(values['date'])} в {values['time']}\n"
        f"💬 Комментарий: {values['comment'] or '—'}"
    )
    return web.Response(
        text=thanks_html(lead_id, service, values["date"], values["time"]),
        content_type="text/html",
    )


async def page_cancel_form(request: web.Request) -> web.Response:
    return web.Response(text=CANCEL_FORM_HTML, content_type="text/html")


async def page_cancel_list(request: web.Request) -> web.Response:
    """Список активных записей клиента по телефону с кнопками отмены."""
    data = await request.post()
    phone = (data.get("phone") or "").strip()
    if not validate_phone(phone):
        return web.Response(
            text=CANCEL_FORM_HTML.replace(
                "</form>",
                "<p class='err'>Проверьте телефон: от 10 до 11 цифр.</p></form>",
            ),
            content_type="text/html",
        )
    digits = phone_digits(phone)
    db = request.app["db"]
    async with db.execute(
        "SELECT id, service, date, time, phone FROM leads "
        "WHERE status = 'active' ORDER BY id DESC"
    ) as cursor:
        rows = await cursor.fetchall()
    mine = [r for r in rows if phone_digits(r[4]) == digits]
    if not mine:
        return web.Response(
            text=CANCEL_FORM_HTML.replace(
                "</form>",
                "<p class='err'>Активных записей на этот телефон не найдено.</p></form>",
            ),
            content_type="text/html",
        )
    items = "".join(
        f"""<form method="post" action="/cancel_do">
<input type="hidden" name="id" value="{r[0]}">
<input type="hidden" name="phone" value="{html.escape(phone)}">
<button class="small" type="submit">Отменить №{r[0]} · {html.escape(r[1])},
{human_date(r[2])} в {r[3]}</button></form>"""
        for r in mine
    )
    return web.Response(
        text=f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Отмена записи</title><style>{STYLE}</style></head><body>
<div class="card">
<h1>Ваши активные записи</h1>
<p class="sub">Нажмите на запись, которую нужно отменить.</p>
{items}
</div></body></html>""",
        content_type="text/html",
    )


async def page_cancel_do(request: web.Request) -> web.Response:
    """Отмена конкретной записи после проверки телефона."""
    data = await request.post()
    phone = (data.get("phone") or "").strip()
    try:
        lead_id = int(data.get("id") or 0)
    except ValueError:
        lead_id = 0
    db = request.app["db"]
    async with db.execute(
        "SELECT id, name, phone, service, date, time FROM leads WHERE id = ?",
        (lead_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or phone_digits(row[2]) != phone_digits(phone) or not validate_phone(phone):
        return web.Response(
            text=CANCEL_FORM_HTML.replace(
                "</form>",
                "<p class='err'>Запись не найдена или телефон не совпадает.</p></form>",
            ),
            content_type="text/html",
        )
    await db.execute(
        "UPDATE leads SET status = 'cancelled' WHERE id = ? AND status = 'active'",
        (lead_id,),
    )
    await db.commit()
    await send_tg(
        f"⚠️ Клиент отменил запись №{row[0]}: {row[3]}, "
        f"{human_date(row[4])} в {row[5]}. Слот снова свободен."
    )
    return web.Response(
        text=f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Запись отменена</title><style>{STYLE}</style></head><body>
<div class="card">
<h1 class="ok">Запись №{row[0]} отменена</h1>
<p class="sub">{html.escape(row[3])}, {human_date(row[4])} в {row[5]}.
Слот снова свободен, владелец получил уведомление.</p>
</div></body></html>""",
        content_type="text/html",
    )


async def page_leads(request: web.Request) -> web.Response:
    db = request.app["db"]
    async with db.execute(
        """
        SELECT id, name, phone, service, date, time, comment, status, created_at
        FROM leads ORDER BY id DESC
        """
    ) as cursor:
        rows = await cursor.fetchall()
    cells = []
    for r in rows:
        cls = " class='cancelled'" if r[7] == "cancelled" else ""
        status = "отменена" if r[7] == "cancelled" else "активна"
        cells.append(
            "<tr{cls}><td>{id}</td><td>{name}</td><td>{phone}</td><td>{service}</td>"
            "<td>{date}</td><td>{time}</td><td>{comment}</td><td>{status}</td>"
            "<td>{created}</td></tr>".format(
                cls=cls, id=r[0], name=html.escape(r[1]), phone=html.escape(r[2]),
                service=html.escape(r[3]), date=human_date(r[4]), time=r[5],
                comment=html.escape(r[6]), status=status, created=r[8],
            )
        )
    body = "".join(cells) or (
        "<tr><td colspan='9'>Заявок пока нет — отправьте первую с главной страницы.</td></tr>"
    )
    return web.Response(text=LEADS_HTML.replace("{ROWS}", body), content_type="text/html")


async def export_csv(request: web.Request) -> web.Response:
    db = request.app["db"]
    async with db.execute(
        """
        SELECT id, name, phone, service, date, time, comment, status, created_at
        FROM leads ORDER BY id
        """
    ) as cursor:
        rows = await cursor.fetchall()
    buf = io.StringIO()
    # Разделитель ";" и BOM: русский Excel открывает файл по колонкам и без кракозябр
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        ["id", "name", "phone", "service", "date", "time", "comment", "status", "created_at"]
    )
    writer.writerows(rows)
    return web.Response(
        text="﻿" + buf.getvalue(),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


async def export_xlsx(request: web.Request) -> web.Response:
    """XLSX-выгрузка: телефон и дата текстом, колонки нужной ширины."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    db = request.app["db"]
    async with db.execute(
        """
        SELECT id, name, phone, service, date, time, comment, status, created_at
        FROM leads ORDER BY id
        """
    ) as cursor:
        rows = await cursor.fetchall()
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    headers = ["№", "Имя", "Телефон", "Услуга", "Дата", "Время",
               "Комментарий", "Статус", "Когда создана"]
    ws.append(headers)
    for r in rows:
        ws.append([r[0], r[1], str(r[2]), r[3], human_date(r[4]), r[5],
                   r[6], "отменена" if r[7] == "cancelled" else "активна", r[8]])
    # Телефон и дата — текстом, чтобы Excel не превращал их в числа и даты
    for row in ws.iter_rows(min_row=2):
        row[2].number_format = "@"
        row[4].number_format = "@"
    widths = [6, 18, 20, 26, 12, 8, 24, 12, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return web.Response(
        body=buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=leads.xlsx"},
    )


app = web.Application()
app.on_startup.append(init_db)
app.on_cleanup.append(close_db)
app.router.add_get("/", page_form)
app.router.add_post("/submit", submit)
app.router.add_get("/slots", page_slots)
app.router.add_get("/leads", page_leads)
app.router.add_get("/export.csv", export_csv)
app.router.add_get("/export.xlsx", export_xlsx)
app.router.add_get("/cancel", page_cancel_form)
app.router.add_post("/cancel", page_cancel_list)
app.router.add_post("/cancel_do", page_cancel_do)

if __name__ == "__main__":
    print(f"Демо запущено: форма http://{HOST}:{PORT}, заявки http://{HOST}:{PORT}/leads")
    web.run_app(app, host=HOST, port=PORT, print=None)
