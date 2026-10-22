# -*- coding: utf-8 -*-
"""
AutomationDemo — демо автоматизации «форма на сайте → база → уведомление в Telegram».

Запуск: python server.py
Затем открыть в браузере: http://127.0.0.1:8080
Страница заявок (аналог таблицы): http://127.0.0.1:8080/leads
Выгрузка в CSV:                  http://127.0.0.1:8080/export.csv
"""
import csv
import html
import io
import os
import re
from datetime import datetime

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
button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:8px;cursor:pointer;
       background:#2aabee;color:#fff;font-size:16px;font-weight:600}
.err{color:#ff7b72;margin-top:12px}
.ok{color:#7ee2a8;font-size:15px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid #2a466b;text-align:left}
th{color:#2aabee}
a{color:#2aabee}
"""

FORM_HTML = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Заявка — демо автоматизации</title><style>{STYLE}</style></head><body>
<div class="card">
<h1>Запись на консультацию</h1>
<p class="sub">Демо-форма: заявка упадёт в базу, на страницу /leads и мгновенно в Telegram владельцу.</p>
<form method="post" action="/submit">
<label>Имя</label><input name="name" placeholder="Анна" required>
<label>Телефон</label><input name="phone" placeholder="+7 999 123-45-67" required>
<label>Услуга</label>
<select name="service">
<option>Консультация</option><option>Диагностика</option><option>Процедура</option>
</select>
<label>Комментарий</label><textarea name="comment" rows="3" placeholder="Необязательно"></textarea>
<button type="submit">Отправить заявку</button>
</form></div></body></html>"""

THANKS_HTML = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Заявка принята</title><style>{STYLE}</style></head><body>
<div class="card">
<h1 class="ok">Заявка принята</h1>
<p class="sub">Владелец уже получил уведомление в Telegram. Посмотреть все заявки:
<a href="/leads">/leads</a></p></div></body></html>"""

LEADS_HTML = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Заявки</title><style>{STYLE}</style></head><body>
<div class="card" style="max-width:900px">
<h1>Заявки</h1>
<p class="sub">То, что владелец видит вместо потерянных сообщений. <a href="/export.csv">Скачать CSV</a></p>
<table><tr><th>№</th><th>Имя</th><th>Телефон</th><th>Услуга</th><th>Комментарий</th><th>Когда</th></tr>
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
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    print("Telegram ответил ошибкой:", resp.status)
    except Exception as exc:  # не роняем сервер из-за сбоя уведомления
        print("Не удалось отправить уведомление:", exc)


async def init_db(app: web.Application) -> None:
    app["db"] = await aiosqlite.connect(DB_PATH)
    await app["db"].execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            service TEXT NOT NULL,
            comment TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    await app["db"].commit()


async def close_db(app: web.Application) -> None:
    await app["db"].close()


async def page_form(request: web.Request) -> web.Response:
    return web.Response(text=FORM_HTML, content_type="text/html")


async def submit(request: web.Request) -> web.Response:
    data = await request.post()
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    service = (data.get("service") or "").strip()
    comment = (data.get("comment") or "").strip()
    digits = re.sub(r"\D", "", phone)
    if len(name) < 2 or len(digits) < 10:
        bad = FORM_HTML.replace(
            "</form>", "<p class='err'>Проверьте имя и телефон: нужно минимум 10 цифр.</p></form>"
        )
        return web.Response(text=bad, content_type="text/html")
    created_at = datetime.now().isoformat(timespec="seconds")
    db = request.app["db"]
    await db.execute(
        "INSERT INTO leads (name, phone, service, comment, created_at) VALUES (?,?,?,?,?)",
        (name, phone, service, comment, created_at),
    )
    await db.commit()
    await send_tg(
        "🔔 Новая заявка с сайта!\n\n"
        f"👤 Имя: {name}\n📞 Телефон: {phone}\n"
        f"💼 Услуга: {service}\n💬 Комментарий: {comment or '—'}"
    )
    return web.Response(text=THANKS_HTML, content_type="text/html")


async def page_leads(request: web.Request) -> web.Response:
    db = request.app["db"]
    async with db.execute(
        "SELECT id, name, phone, service, comment, created_at FROM leads ORDER BY id DESC"
    ) as cursor:
        rows = await cursor.fetchall()
    body = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            r[0], html.escape(r[1]), html.escape(r[2]), html.escape(r[3]),
            html.escape(r[4]), r[5],
        )
        for r in rows
    ) or "<tr><td colspan='6'>Заявок пока нет — отправьте первую с главной страницы.</td></tr>"
    return web.Response(text=LEADS_HTML.replace("{ROWS}", body), content_type="text/html")


async def export_csv(request: web.Request) -> web.Response:
    db = request.app["db"]
    async with db.execute(
        "SELECT id, name, phone, service, comment, created_at FROM leads ORDER BY id"
    ) as cursor:
        rows = await cursor.fetchall()
    buf = io.StringIO()
    # Разделитель ";" и BOM: русский Excel открывает файл по колонкам и без кракозябр
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["id", "name", "phone", "service", "comment", "created_at"])
    writer.writerows(rows)
    return web.Response(
        text="\ufeff" + buf.getvalue(),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


app = web.Application()
app.on_startup.append(init_db)
app.on_cleanup.append(close_db)
app.router.add_get("/", page_form)
app.router.add_post("/submit", submit)
app.router.add_get("/leads", page_leads)
app.router.add_get("/export.csv", export_csv)

if __name__ == "__main__":
    print(f"Демо запущено: форма http://{HOST}:{PORT}, заявки http://{HOST}:{PORT}/leads")
    web.run_app(app, host=HOST, port=PORT, print=None)
