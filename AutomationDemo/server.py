# -*- coding: utf-8 -*-
"""
AutomationDemo — сайт записи: форма → подтверждение → база → уведомление в Telegram.

Клиентский путь: форма → экран подтверждения с возможностью исправить →
номер записи → кабинет «Мои записи» (просмотр и отмена по телефону).
Путь владельца: кабинет администратора /admin (ключ из .env) → таблица заявок
и выгрузки CSV/XLSX.

Слот занят под конкретную услугу, как в боте BotRecord; каталог услуг,
даты и слоты — те же. Вёрстка адаптивная: сайт удобно пользоваться с телефона.

Запуск: python server.py
Сайт:   http://127.0.0.1:8080
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
# Ключ входа в кабинет владельца (демо-авторизация; в бою — нормальный логин)
ADMIN_KEY = os.getenv("ADMIN_KEY", "botrecord-demo").strip()

DB_PATH = os.path.join(BASE_DIR, "leads.db")
HOST, PORT = "127.0.0.1", 8080

# Каталог услуг и слоты — те же, что у бота BotRecord.
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
*{box-sizing:border-box}
body{font-family:Segoe UI,Arial,sans-serif;background:#0e1626;color:#f0f4f8;
     margin:0;min-height:100vh;display:flex;flex-direction:column}
.wrap{width:100%;max-width:1040px;margin:0 auto;padding:0 16px}
.topbar{background:#0b1220;border-bottom:1px solid #1d3050}
.topbar .wrap{display:flex;align-items:center;justify-content:space-between;
     height:56px}
.brand{font-weight:700;letter-spacing:.5px;color:#2aabee}
.brand span{color:#f0f4f8}
nav a{color:#9fb0c4;text-decoration:none;margin-left:22px;font-size:14px}
nav a:hover{color:#2aabee}
main{flex:1;display:flex;justify-content:center;padding:40px 16px}
.card{background:#12233c;border-radius:16px;padding:36px 40px;max-width:560px;
      width:100%;box-shadow:0 12px 44px rgba(0,0,0,.45);height:fit-content}
.card.wide{max-width:1040px}
h1{font-size:26px;margin:0 0 8px}
.sub{color:#9fb0c4;margin:0 0 26px;font-size:14px;line-height:1.55}
label{display:block;margin:14px 0 6px;font-size:14px;color:#c6d2e0}
input,select,textarea{width:100%;padding:11px 13px;border-radius:9px;
      border:1px solid #2a466b;background:#0b1220;color:#f0f4f8;font-size:15px}
textarea{resize:vertical;max-width:100%}
select option:disabled{color:#5b6b80}
.row{display:flex;gap:12px}
.row>div{flex:1}
.btn{display:inline-block;margin-top:20px;width:100%;padding:13px;border:0;
     border-radius:9px;cursor:pointer;background:#2aabee;color:#fff;
     font-size:16px;font-weight:600;text-align:center;text-decoration:none}
.btn:hover{background:#3fb9f5}
.btn.ghost{background:transparent;border:1px solid #2a466b;color:#c6d2e0;
     font-weight:500}
.btn.ghost:hover{border-color:#2aabee;color:#2aabee}
.btn.danger{background:transparent;border:1px solid #ff7b72;color:#ff7b72;
     width:auto;margin:6px 6px 0 0;padding:9px 16px;font-size:14px}
.btnrow{display:flex;gap:10px}
.btnrow .btn{margin-top:16px}
.err{color:#ff7b72;margin:14px 0 0;font-size:14px}
.ok{color:#7ee2a8}
.num{color:#2aabee;font-weight:700}
.summary{background:#0b1220;border:1px solid #2a466b;border-radius:10px;
     padding:16px 18px;margin:18px 0 4px;font-size:15px;line-height:1.7}
.summary b{color:#2aabee;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:9px 10px;border-bottom:1px solid #223c60;text-align:left}
th{color:#2aabee;font-weight:600}
.cancelled{color:#8494a8;text-decoration:line-through}
footer .wrap{padding:18px 16px 26px;color:#5b6b80;font-size:13px}
footer a{color:#5b6b80}
@media (max-width:600px){
  main{padding:20px 10px}
  .card{padding:24px 20px}
  .row{flex-direction:column;gap:0}
  .btnrow{flex-direction:column}
  nav a{margin-left:12px;font-size:13px}
  table{font-size:12px}
  th,td{padding:6px 5px}
}
"""

HEADER = """<header class="topbar"><div class="wrap">
<span class="brand">● <span>BotRecord</span> · онлайн-запись</span>
<nav><a href="/">Записаться</a><a href="/my">Мои записи</a><a href="/admin">Владельцу</a></nav>
</div></header>"""

FOOTER = """<footer><div class="wrap">
Демо связки «форма → база → уведомление в Telegram». Слот блокируется под
каждую услугу отдельно, двойная запись исключена.
<a href="/admin">Кабинет владельца</a> · <a href="/my">Кабинет клиента</a>
</div></footer>"""


def page_shell(title: str, body: str, wide: bool = False) -> str:
    """Общая обёртка страниц: шапка, карточка, подвал, адаптивная вёрстка."""
    cls = ' class="card wide"' if wide else ' class="card"'
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{STYLE}</style></head><body>
{HEADER}
<main><div{cls}>{body}</div></main>
{FOOTER}
</body></html>"""


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
    """Телефон по правилам РФ: 11 цифр с 7/8 либо 10 цифр с 9."""
    digits = phone_digits(phone)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return True
    if len(digits) == 10 and digits[0] == "9":
        return True
    return False


PHONE_HINT = ("Введите номер РФ: 11 цифр, начинающихся с 7 или 8 "
              "(+7 999 123-45-67), либо 10 цифр, начинающихся с 9.")


def form_html(error: str = "", values: dict = None) -> str:
    """Форма записи. При ошибке или возврате с подтверждения значения сохраняются."""
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
        f'<option value="{sid}"'
        f'{" selected" if str(values.get("service")) == str(sid) else ""}>'
        f'{s["name"]} — {s["price"]} ₽</option>'
        for sid, s in SERVICES.items()
    )
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    name_v = html.escape(values.get("name", ""))
    phone_v = html.escape(values.get("phone", ""))
    comment_v = html.escape(values.get("comment", ""))
    body = f"""
<h1>Онлайн-запись</h1>
<p class="sub">Выберите услугу и свободное время — заявка уйдёт владельцу
мгновенно, а вы получите номер записи. Занятое время блокируется под каждую
услугу отдельно.</p>
{err}
<form method="post" action="/submit">
<label>Имя</label><input name="name" placeholder="Анна" value="{name_v}" required>
<label>Телефон</label><input name="phone" inputmode="tel"
 placeholder="+7 999 123-45-67" value="{phone_v}" required>
<label>Услуга</label><select name="service" id="service">{service_opts}</select>
<div class="row"><div>
<label>Дата</label><select name="date" id="date">{dates_opts}</select>
</div><div>
<label>Время</label><select name="time" id="time">{time_opts}</select>
</div></div>
<label>Комментарий</label><textarea name="comment" rows="3"
placeholder="Необязательно">{comment_v}</textarea>
<button class="btn" type="submit">Продолжить к подтверждению</button>
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
</script>"""
    return page_shell("Запись — демо автоматизации", body)


def hidden_inputs(values: dict) -> str:
    return "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(str(values.get(k, "")))}">'
        for k in ("name", "phone", "service", "comment", "date", "time")
    )


def confirm_html(values: dict) -> str:
    """Экран подтверждения: сводка, кнопки «Подтвердить» и «Изменить»."""
    service = SERVICES[int(values["service"])]
    body = f"""
<h1>Проверьте запись</h1>
<p class="sub">Если всё верно — подтверждайте. Ошиблись — вернитесь
и исправьте, данные сохранятся.</p>
<div class="summary">
<b>Услуга:</b> {service['name']} — {service['price']} ₽<br>
<b>Когда:</b> {human_date(values['date'])} в {values['time']}<br>
<b>Имя:</b> {html.escape(values['name'])}<br>
<b>Телефон:</b> {html.escape(values['phone'])}<br>
<b>Комментарий:</b> {html.escape(values['comment']) or '—'}
</div>
<div class="btnrow">
<form method="post" action="/confirm_do" style="flex:1">
{hidden_inputs(values)}
<button class="btn" type="submit">Подтвердить запись</button>
</form>
<form method="post" action="/edit" style="flex:1">
{hidden_inputs(values)}
<button class="btn ghost" type="submit">Изменить данные</button>
</form>
</div>"""
    return page_shell("Подтверждение записи", body)


def thanks_html(lead_id: int, service: str, date_iso: str, time_str: str) -> str:
    """Страница клиента после записи: номер, слот и понятные следующие шаги."""
    body = f"""
<h1 class="ok">Заявка принята</h1>
<p class="sub">Номер вашей записи: <span class="num">№{lead_id}</span><br>
{html.escape(service)}, {human_date(date_iso)} в {time_str}.<br>
Владелец уже получил уведомление. Если планы изменятся — отмените запись
в кабинете «Мои записи» по номеру телефона.</p>
<div class="btnrow">
<a class="btn" href="/">Новая заявка</a>
<a class="btn ghost" href="/my">Мои записи</a>
</div>"""
    return page_shell("Заявка принята", body)


def my_form_html(error: str = "") -> str:
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    body = f"""
<h1>Мои записи</h1>
<p class="sub">Введите телефон, на который записывались, — покажем активные
записи: посмотреть детали или отменить.</p>
{err}
<form method="post" action="/my">
<label>Телефон</label><input name="phone" inputmode="tel"
 placeholder="+7 999 123-45-67" required>
<button class="btn" type="submit">Показать мои записи</button>
</form>"""
    return page_shell("Мои записи", body)


def my_list_html(phone: str, rows: list) -> str:
    items = "".join(
        f"""<div class="summary">
<b>№{r[0]}</b> · {html.escape(r[1])}<br>
{human_date(r[2])} в {r[3]}<br>
Комментарий: {html.escape(r[4]) or '—'}
<form method="post" action="/cancel_do">
<input type="hidden" name="id" value="{r[0]}">
<input type="hidden" name="phone" value="{html.escape(phone)}">
<button class="btn danger" type="submit">Отменить эту запись</button>
</form></div>"""
        for r in rows
    )
    body = f"""
<h1>Ваши активные записи</h1>
<p class="sub">Найдите нужную и отмените одним нажатием — слот сразу
освободится для других клиентов.</p>
{items}
<div class="btnrow">
<a class="btn" href="/">Записаться ещё</a>
</div>"""
    return page_shell("Мои записи", body)


def cancel_done_html(row) -> str:
    body = f"""
<h1 class="ok">Запись №{row[0]} отменена</h1>
<p class="sub">{html.escape(row[1])}, {human_date(row[2])} в {row[3]}.
Слот снова свободен, владелец получил уведомление.</p>
<div class="btnrow">
<a class="btn" href="/">Записаться снова</a>
<a class="btn ghost" href="/my">Мои записи</a>
</div>"""
    return page_shell("Запись отменена", body)


def admin_form_html(error: str = "") -> str:
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    body = f"""
<h1>Кабинет владельца</h1>
<p class="sub">Введите ключ доступа из файла .env (поле ADMIN_KEY) —
откроются таблица заявок и выгрузки CSV/XLSX.</p>
{err}
<form method="post" action="/admin">
<label>Ключ доступа</label><input name="key" type="password" required>
<button class="btn" type="submit">Войти</button>
</form>"""
    return page_shell("Кабинет владельца", body)


def admin_table_html(key: str, rows: list) -> str:
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
    table = "".join(cells) or (
        "<tr><td colspan='9'>Заявок пока нет — отправьте первую с главной.</td></tr>"
    )
    body = f"""
<h1>Заявки</h1>
<p class="sub">То, что владелец видит вместо потерянных сообщений.
Выгрузки: <a href="/export.csv?key={html.escape(key)}">CSV</a> ·
<a href="/export.xlsx?key={html.escape(key)}">XLSX</a></p>
<table><tr><th>№</th><th>Имя</th><th>Телефон</th><th>Услуга</th><th>Дата</th>
<th>Время</th><th>Комментарий</th><th>Статус</th><th>Создана</th></tr>
{table}</table>"""
    return page_shell("Заявки владельца", body, wide=True)


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
                # Короткий таймаут: сайт не должен висеть, если Telegram недоступен
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    print("Telegram ответил ошибкой:", resp.status)
    except Exception as exc:  # не роняем сервер из-за сбоя уведомления
        print("Не удалось отправить уведомление:", exc)


async def init_db(app: web.Application) -> None:
    app["db"] = await aiosqlite.connect(DB_PATH)
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
    async with db.execute(
        "SELECT time FROM leads WHERE date = ? AND service = ? AND status = 'active'",
        (date_iso, service),
    ) as cursor:
        busy = {row[0] for row in await cursor.fetchall()}
    return sorted(t for t in TIME_SLOTS if t in busy or is_past_slot(date_iso, t))


def read_form(data) -> dict:
    return {
        "name": (data.get("name") or "").strip(),
        "phone": (data.get("phone") or "").strip(),
        "service": (data.get("service") or "").strip(),
        "comment": (data.get("comment") or "").strip(),
        "date": (data.get("date") or "").strip(),
        "time": (data.get("time") or "").strip(),
    }


def check_values(values: dict):
    """Возвращает текст ошибки или None, если значения корректны."""
    if len(values["name"]) < 2:
        return "Проверьте имя: минимум 2 символа."
    if not validate_phone(values["phone"]):
        return "Проверьте телефон. " + PHONE_HINT
    if not (values["service"].isdigit() and int(values["service"]) in SERVICES):
        return "Выберите услугу из списка."
    if values["date"] not in valid_dates() or values["time"] not in TIME_SLOTS:
        return "Некорректные дата или время. Выберите из списка."
    return None


async def page_form(request: web.Request) -> web.Response:
    return web.Response(text=form_html(), content_type="text/html")


async def page_slots(request: web.Request) -> web.Response:
    date_iso = request.query.get("date", "")
    service_id = request.query.get("service", "")
    service = SERVICES.get(int(service_id), {}).get("name") \
        if service_id.isdigit() else None
    if date_iso not in valid_dates() or service is None:
        return web.json_response([])
    busy = await get_busy_times(request.app["db"], date_iso, service)
    return web.json_response(busy)


async def submit(request: web.Request) -> web.Response:
    """Шаг 1: проверка значений и показ экрана подтверждения."""
    values = read_form(await request.post())
    error = check_values(values)
    if error:
        return web.Response(text=form_html(error, values), content_type="text/html")
    return web.Response(text=confirm_html(values), content_type="text/html")


async def edit(request: web.Request) -> web.Response:
    """Кнопка «Изменить данные» на экране подтверждения."""
    values = read_form(await request.post())
    return web.Response(text=form_html("", values), content_type="text/html")


async def confirm_do(request: web.Request) -> web.Response:
    """Шаг 2: повторная проверка, запись в базу, уведомление владельцу."""
    values = read_form(await request.post())
    error = check_values(values)
    if error:
        return web.Response(text=form_html(error, values), content_type="text/html")
    service = SERVICES[int(values["service"])]["name"]
    db = request.app["db"]
    if values["time"] in await get_busy_times(db, values["date"], service):
        return web.Response(
            text=form_html(
                f"Время {values['time']} на {human_date(values['date'])} под услугу "
                f"«{service}» только что заняли. Выберите другой слот.", values,
            ),
            content_type="text/html",
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
        return web.Response(
            text=form_html(
                f"Время {values['time']} на {human_date(values['date'])} под услугу "
                f"«{service}» только что заняли. Выберите другой слот.", values,
            ),
            content_type="text/html",
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


async def page_my_form(request: web.Request) -> web.Response:
    return web.Response(text=my_form_html(), content_type="text/html")


async def page_my_list(request: web.Request) -> web.Response:
    phone = ((await request.post()).get("phone") or "").strip()
    if not validate_phone(phone):
        return web.Response(
            text=my_form_html("Проверьте телефон. " + PHONE_HINT),
            content_type="text/html",
        )
    digits = phone_digits(phone)
    db = request.app["db"]
    async with db.execute(
        "SELECT id, service, date, time, comment, phone FROM leads "
        "WHERE status = 'active' ORDER BY date, time"
    ) as cursor:
        rows = await cursor.fetchall()
    mine = [r for r in rows if phone_digits(r[5]) == digits]
    if not mine:
        return web.Response(
            text=my_form_html("Активных записей на этот телефон не найдено."),
            content_type="text/html",
        )
    return web.Response(
        text=my_list_html(phone, mine), content_type="text/html"
    )


async def page_cancel_do(request: web.Request) -> web.Response:
    data = await request.post()
    phone = (data.get("phone") or "").strip()
    try:
        lead_id = int(data.get("id") or 0)
    except ValueError:
        lead_id = 0
    db = request.app["db"]
    async with db.execute(
        "SELECT id, service, date, time, phone FROM leads WHERE id = ?",
        (lead_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or phone_digits(row[4]) != phone_digits(phone):
        return web.Response(
            text=my_form_html("Запись не найдена или телефон не совпадает."),
            content_type="text/html",
        )
    await db.execute(
        "UPDATE leads SET status = 'cancelled' WHERE id = ? AND status = 'active'",
        (lead_id,),
    )
    await db.commit()
    await send_tg(
        f"⚠️ Клиент отменил запись №{row[0]}: {row[1]}, "
        f"{human_date(row[2])} в {row[3]}. Слот снова свободен."
    )
    return web.Response(text=cancel_done_html(row), content_type="text/html")


async def page_admin_form(request: web.Request) -> web.Response:
    return web.Response(text=admin_form_html(), content_type="text/html")


async def page_admin(request: web.Request) -> web.Response:
    key = ((await request.post()).get("key") or "").strip()
    if key != ADMIN_KEY:
        return web.Response(
            text=admin_form_html("Неверный ключ доступа."),
            content_type="text/html",
        )
    db = request.app["db"]
    async with db.execute(
        """
        SELECT id, name, phone, service, date, time, comment, status, created_at
        FROM leads ORDER BY id DESC
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return web.Response(
        text=admin_table_html(key, rows), content_type="text/html"
    )


def check_admin_key(request: web.Request) -> bool:
    return request.query.get("key", "") == ADMIN_KEY


async def page_leads(request: web.Request) -> web.Response:
    """Прямая ссылка на таблицу — только с ключом (иначе уходим на вход)."""
    if not check_admin_key(request):
        return web.Response(
            text=admin_form_html("Откройте таблицу через кабинет владельца."),
            content_type="text/html",
        )
    db = request.app["db"]
    async with db.execute(
        """
        SELECT id, name, phone, service, date, time, comment, status, created_at
        FROM leads ORDER BY id DESC
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return web.Response(
        text=admin_table_html(request.query["key"], rows), content_type="text/html"
    )


async def export_rows(db):
    async with db.execute(
        """
        SELECT id, name, phone, service, date, time, comment, status, created_at
        FROM leads ORDER BY id
        """
    ) as cursor:
        return await cursor.fetchall()


async def export_csv(request: web.Request) -> web.Response:
    if not check_admin_key(request):
        return web.Response(status=403, text="Нужен ключ: откройте /admin")
    rows = await export_rows(request.app["db"])
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
    if not check_admin_key(request):
        return web.Response(status=403, text="Нужен ключ: откройте /admin")
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    rows = await export_rows(request.app["db"])
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    ws.append(["№", "Имя", "Телефон", "Услуга", "Дата", "Время",
               "Комментарий", "Статус", "Когда создана"])
    for r in rows:
        ws.append([r[0], r[1], str(r[2]), r[3], human_date(r[4]), r[5],
                   r[6], "отменена" if r[7] == "cancelled" else "активна", r[8]])
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
app.router.add_post("/edit", edit)
app.router.add_post("/confirm_do", confirm_do)
app.router.add_get("/slots", page_slots)
app.router.add_get("/my", page_my_form)
app.router.add_post("/my", page_my_list)
app.router.add_post("/cancel_do", page_cancel_do)
app.router.add_get("/admin", page_admin_form)
app.router.add_post("/admin", page_admin)
app.router.add_get("/leads", page_leads)
app.router.add_get("/export.csv", export_csv)
app.router.add_get("/export.xlsx", export_xlsx)

if __name__ == "__main__":
    print(f"Демо запущено: сайт http://{HOST}:{PORT}, кабинет владельца /admin")
    web.run_app(app, host=HOST, port=PORT, print=None)
