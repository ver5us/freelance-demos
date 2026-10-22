# -*- coding: utf-8 -*-
"""
BotRecord — Telegram-бот записи для малого бизнеса.

Демо-проект для портфолио: салон / клиника / барбершоп / фитнес / автосервис.
Стек: aiogram 3.x + aiosqlite (SQLite) + python-dotenv.

Запуск: python bot.py
"""

import asyncio
import html
import logging
import os
import re
from datetime import date, datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

# ============================================================
# 1. КОНФИГУРАЦИЯ
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Скопируйте .env.example в .env "
        "и впишите токен, полученный у @BotFather."
    )

_raw_admin_id = os.getenv("ADMIN_CHAT_ID", "").strip()
# ADMIN_CHAT_ID может быть пустым — тогда уведомления админа печатаются в консоль.
ADMIN_CHAT_ID = int(_raw_admin_id) if _raw_admin_id.lstrip("-").isdigit() else None

# Прокси для доступа к api.telegram.org (если провайдер блокирует Telegram).
# Формат в .env: PROXY_URL=http://123.45.67.89:8080
# Пустое значение — бот работает напрямую, без прокси.
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

DB_PATH = os.getenv("DB_PATH", "bookings.db")

# Каталог услуг демо-бота. Под реального клиента меняется за пару минут.
SERVICES = {
    1: {"name": "Стрижка", "price": 1500},
    2: {"name": "Маникюр", "price": 2000},
    3: {"name": "Консультация специалиста", "price": 1000},
    4: {"name": "Уходовая процедура", "price": 2500},
}

# Слоты записи: с 09:00 до 20:00 с шагом 1 час (последний слот — 19:00–20:00).
TIME_SLOTS = [f"{hour:02d}:00" for hour in range(9, 20)]

# На сколько дней вперёд открыта запись.
DAYS_AHEAD = 7

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Если PROXY_URL задан в .env — идём через прокси, иначе напрямую.
_session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=_session,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ============================================================
# 2. БАЗА ДАННЫХ (SQLite через aiosqlite)
# ============================================================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    service_id INTEGER NOT NULL,
    service_name TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
"""


async def init_db() -> None:
    """Создаёт таблицу записей, если её ещё нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()


async def get_booked_times(date_iso: str) -> set:
    """Возвращает множество занятых слотов времени на указанную дату."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT time FROM bookings WHERE date = ? AND status = 'active'",
            (date_iso,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def is_slot_taken(date_iso: str, time_str: str) -> bool:
    """Проверяет, занят ли слот (дата + время) активной записью."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM bookings WHERE date = ? AND time = ? AND status = 'active'",
            (date_iso, time_str),
        ) as cursor:
            return await cursor.fetchone() is not None


async def create_booking(
    telegram_user_id: int,
    name: str,
    phone: str,
    service_id: int,
    service_name: str,
    date_iso: str,
    time_str: str,
) -> int:
    """Сохраняет запись в базу и возвращает её id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO bookings
                (telegram_user_id, name, phone, service_id, service_name,
                 date, time, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                telegram_user_id,
                name,
                phone,
                service_id,
                service_name,
                date_iso,
                time_str,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_active_bookings(telegram_user_id: int) -> list:
    """Все активные записи пользователя, отсортированные по дате и времени."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM bookings
            WHERE telegram_user_id = ? AND status = 'active'
            ORDER BY date, time
            """,
            (telegram_user_id,),
        ) as cursor:
            return await cursor.fetchall()


async def cancel_last_active_booking(telegram_user_id: int):
    """Отменяет последнюю активную запись пользователя.

    Возвращает словарь с данными отменённой записи или None,
    если активных записей нет.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM bookings
            WHERE telegram_user_id = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT 1
            """,
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        await db.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ?",
            (row["id"],),
        )
        await db.commit()
        return dict(row)


async def get_bookings_for_date(date_iso: str) -> list:
    """Все активные записи на дату (для команды админа /today)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM bookings
            WHERE date = ? AND status = 'active'
            ORDER BY time
            """,
            (date_iso,),
        ) as cursor:
            return await cursor.fetchall()


# ============================================================
# 3. СОСТОЯНИЯ FSM
# ============================================================

class BookingForm(StatesGroup):
    """Состояния диалога записи."""

    service = State()   # выбор услуги
    date = State()      # выбор даты
    time = State()      # выбор времени
    name = State()      # ввод имени
    phone = State()     # ввод телефона
    confirm = State()   # подтверждение записи


# ============================================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def human_date(date_iso: str) -> str:
    """Превращает '2026-10-20' в человекочитаемое 'Пн, 20.10'."""
    d = date.fromisoformat(date_iso)
    return f"{WEEKDAYS_RU[d.weekday()]}, {d.strftime('%d.%m')}"


def valid_dates() -> list:
    """Список ISO-дат, доступных для записи: сегодня + DAYS_AHEAD дней."""
    today = date.today()
    return [(today + timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD)]


def is_past_slot(date_iso: str, time_str: str) -> bool:
    """True, если слот относится к сегодняшнему дню и уже прошёл."""
    if date_iso != date.today().isoformat():
        return False
    slot_hour = int(time_str.split(":")[0])
    return slot_hour <= datetime.now().hour


def validate_name(text: str):
    """Проверяет имя. Возвращает очищенное имя или None."""
    name = text.strip()
    if name.startswith("/"):  # пользователь ввёл команду вместо имени
        return None
    if not (2 <= len(name) <= 50):
        return None
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё\s\-]+", name):
        return None
    return name


def validate_phone(text: str):
    """Проверяет телефон. Возвращает телефон как введён или None.

    Допустимы форматы: +7 999 123-45-67, 89991234567, 9991234567.
    Правило одно: не меньше 10 цифр.
    """
    if text.strip().startswith("/"):
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) < 10:
        return None
    return text.strip()


def booking_summary_text(data: dict) -> str:
    """Сводка записи для экрана подтверждения."""
    service = SERVICES[data["service_id"]]
    return (
        "<b>Проверьте данные записи:</b>\n\n"
        f"💼 Услуга: {service['name']} — {service['price']} ₽\n"
        f"📅 Дата: {human_date(data['date'])}\n"
        f"⏰ Время: {data['time']}\n"
        f"👤 Имя: {html.escape(data['name'])}\n"
        f"📞 Телефон: {html.escape(data['phone'])}\n"
    )


def is_admin(user_id: int) -> bool:
    """Проверка прав администратора."""
    return ADMIN_CHAT_ID is not None and user_id == ADMIN_CHAT_ID


async def my_bookings_text(user_id: int) -> str:
    """Текст со списком активных записей пользователя."""
    rows = await get_user_active_bookings(user_id)
    if not rows:
        return (
            "У вас пока нет активных записей.\n"
            "Нажмите «📅 Записаться», чтобы создать первую."
        )
    lines = ["<b>Ваши активные записи:</b>\n"]
    for r in rows:
        lines.append(
            f"• №{r['id']} — {r['service_name']}, "
            f"{human_date(r['date'])} в {r['time']}"
        )
    lines.append("\nОтменить последнюю запись: /cancel")
    return "\n".join(lines)


# ============================================================
# 5. КЛАВИАТУРЫ
# ============================================================

def main_menu_kb() -> InlineKeyboardMarkup:
    """Главное меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Записаться", callback_data="nav:book")],
        [InlineKeyboardButton(text="📋 Мои записи", callback_data="nav:my")],
        [InlineKeyboardButton(text="❌ Отменить запись", callback_data="nav:cancel")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="nav:help")],
    ])


def services_kb() -> InlineKeyboardMarkup:
    """Каталог услуг."""
    buttons = [
        [InlineKeyboardButton(
            text=f"{s['name']} — {s['price']} ₽",
            callback_data=f"service:{service_id}",
        )]
        for service_id, s in SERVICES.items()
    ]
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav:main")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def dates_kb() -> InlineKeyboardMarkup:
    """Календарь на DAYS_AHEAD дней вперёд."""
    buttons = [
        [InlineKeyboardButton(text=human_date(d), callback_data=f"date:{d}")]
        for d in valid_dates()
    ]
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav:main")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def times_kb(booked: set, date_iso: str) -> InlineKeyboardMarkup:
    """Слоты времени по 3 в ряд. Занятые и прошедшие помечены ❌."""
    rows, row = [], []
    for t in TIME_SLOTS:
        if t in booked or is_past_slot(date_iso, t):
            row.append(InlineKeyboardButton(text=f"❌ {t}", callback_data="time:busy"))
        else:
            row.append(InlineKeyboardButton(text=f"🕐 {t}", callback_data=f"time:{t}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav:main")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_only_kb() -> InlineKeyboardMarkup:
    """Только кнопка возврата в главное меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav:main")],
    ])


def confirm_kb() -> InlineKeyboardMarkup:
    """Подтверждение записи."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm:yes"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="confirm:no"),
        ],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    """Подтверждение отмены последней записи."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="❌ Отменить последнюю запись", callback_data="cancel:last"
        )],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav:main")],
    ])


# ============================================================
# 6. ТЕКСТЫ И УВЕДОМЛЕНИЯ
# ============================================================

WELCOME_TEXT = (
    "Здравствуйте! 👋\n\n"
    "Это бот онлайн-записи. Выберите услугу, дату и свободное время — "
    "и вы записаны. Администратор получит уведомление мгновенно.\n\n"
    "Выберите действие:"
)

HELP_TEXT = (
    "<b>Как пользоваться ботом:</b>\n\n"
    "1. Нажмите «📅 Записаться».\n"
    "2. Выберите услугу, дату и свободное время.\n"
    "3. Введите имя и телефон.\n"
    "4. Подтвердите запись.\n\n"
    "<b>Команды:</b>\n"
    "/start — главное меню\n"
    "/my_bookings — мои активные записи\n"
    "/cancel — отменить последнюю запись\n"
    "/help — эта справка"
)


async def notify_admin(text: str) -> None:
    """Отправляет уведомление админу.

    Если ADMIN_CHAT_ID задан — сообщением в Telegram.
    Если не задан — печатает уведомление в консоль (удобно для тестов).
    """
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, text)
        except Exception as exc:  # не роняем бота из-за сбоя уведомления
            logging.error("Не удалось отправить уведомление админу: %s", exc)
    else:
        print("\n===== УВЕДОМЛЕНИЕ АДМИНУ (ADMIN_CHAT_ID не задан) =====")
        print(re.sub(r"</?b>", "", text))
        print("========================================================\n")


async def safe_edit(callback: CallbackQuery, text: str,
                    kb: InlineKeyboardMarkup) -> None:
    """Редактирует сообщение, игнорируя ошибку 'message is not modified'."""
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


# ============================================================
# 7. ХЕНДЛЕРЫ КОМАНД
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@dp.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_menu_kb())


@dp.message(Command("my_bookings"))
async def cmd_my_bookings(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        await my_bookings_text(message.from_user.id),
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Отмена последней активной записи пользователя."""
    await state.clear()
    row = await cancel_last_active_booking(message.from_user.id)
    if row is None:
        await message.answer(
            "У вас нет активных записей — отменять нечего.",
            reply_markup=main_menu_kb(),
        )
        return
    await message.answer(
        f"❌ Запись отменена: {row['service_name']}, "
        f"{human_date(row['date'])} в {row['time']}.",
        reply_markup=main_menu_kb(),
    )
    await notify_admin(
        "<b>⚠️ Клиент отменил запись</b>\n\n"
        f"💼 Услуга: {row['service_name']}\n"
        f"📅 Дата: {human_date(row['date'])}\n"
        f"⏰ Время: {row['time']}\n"
        f"👤 Клиент: {html.escape(row['name'])}\n"
        f"📞 Телефон: {html.escape(row['phone'])}"
    )


@dp.message(Command("today"))
async def cmd_today(message: Message) -> None:
    """Расписание на сегодня — только для администратора."""
    if not is_admin(message.from_user.id):
        await message.answer(
            "Эта команда доступна только администратору.\n"
            "(Администратор задаётся через ADMIN_CHAT_ID в файле .env)"
        )
        return
    today_iso = date.today().isoformat()
    rows = await get_bookings_for_date(today_iso)
    if not rows:
        await message.answer(
            f"На сегодня ({human_date(today_iso)}) активных записей нет."
        )
        return
    lines = [f"<b>Записи на сегодня, {human_date(today_iso)}:</b>\n"]
    for r in rows:
        lines.append(
            f"🕐 {r['time']} — {r['service_name']} — {r['name']}, {r['phone']}"
        )
    await message.answer("\n".join(lines))


# ============================================================
# 8. ХЕНДЛЕРЫ НАВИГАЦИИ (ГЛАВНОЕ МЕНЮ)
# ============================================================

@dp.callback_query(F.data == "nav:main")
async def cb_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(callback, WELCOME_TEXT, main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "nav:book")
async def cb_book(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BookingForm.service)
    await safe_edit(callback, "Выберите услугу:", services_kb())
    await callback.answer()


@dp.callback_query(F.data == "nav:my")
async def cb_my(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        callback,
        await my_bookings_text(callback.from_user.id),
        main_menu_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "nav:help")
async def cb_help(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(callback, HELP_TEXT, main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "nav:cancel")
async def cb_cancel_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает последнюю активную запись и предлагает её отменить."""
    await state.clear()
    rows = await get_user_active_bookings(callback.from_user.id)
    if not rows:
        await safe_edit(
            callback,
            "У вас нет активных записей — отменять нечего.",
            main_menu_kb(),
        )
    else:
        last = max(rows, key=lambda r: r["id"])  # последняя по времени создания
        await safe_edit(
            callback,
            "Отменить последнюю запись?\n\n"
            f"💼 {last['service_name']}\n"
            f"📅 {human_date(last['date'])} в {last['time']}",
            cancel_kb(),
        )
    await callback.answer()


# ============================================================
# 9. ХЕНДЛЕРЫ СЦЕНАРИЯ ЗАПИСИ (FSM)
# ============================================================

@dp.callback_query(StateFilter(BookingForm.service), F.data.startswith("service:"))
async def cb_service(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1: выбрана услуга → показываем даты."""
    try:
        service_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        service_id = 0
    if service_id not in SERVICES:
        await callback.answer("Неизвестная услуга.", show_alert=True)
        return
    await state.update_data(service_id=service_id)
    await state.set_state(BookingForm.date)
    await safe_edit(callback, "Выберите дату:", dates_kb())
    await callback.answer()


@dp.callback_query(StateFilter(BookingForm.date), F.data.startswith("date:"))
async def cb_date(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 2: выбрана дата → показываем свободное время."""
    date_iso = callback.data.split(":", 1)[1]
    if date_iso not in valid_dates():
        await callback.answer(
            "Эта дата недоступна. Выберите другую.", show_alert=True
        )
        return
    booked = await get_booked_times(date_iso)
    free = [t for t in TIME_SLOTS
            if t not in booked and not is_past_slot(date_iso, t)]
    if not free:
        await callback.answer(
            "На эту дату всё занято. Выберите другую.", show_alert=True
        )
        return
    await state.update_data(date=date_iso)
    await state.set_state(BookingForm.time)
    await safe_edit(
        callback,
        f"Дата: {human_date(date_iso)}\nВыберите свободное время:",
        times_kb(booked, date_iso),
    )
    await callback.answer()


@dp.callback_query(F.data == "time:busy")
async def cb_time_busy(callback: CallbackQuery) -> None:
    """Нажатие на занятый/прошедший слот."""
    await callback.answer(
        "Это время уже занято. Выберите другое.", show_alert=True
    )


@dp.callback_query(
    StateFilter(BookingForm.time), F.data.regexp(r"^time:\d{2}:\d{2}$")
)
async def cb_time(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 3: выбрано время → просим имя."""
    time_str = callback.data.split(":", 1)[1]
    data = await state.get_data()
    date_iso = data.get("date")
    if time_str not in TIME_SLOTS or not date_iso:
        await callback.answer(
            "Слот недоступен. Начните запись заново: /start", show_alert=True
        )
        return
    # Повторная проверка: слот могли занять, пока клиент выбирал.
    if is_past_slot(date_iso, time_str) or await is_slot_taken(date_iso, time_str):
        booked = await get_booked_times(date_iso)
        await safe_edit(
            callback,
            f"Дата: {human_date(date_iso)}\nВыберите свободное время:",
            times_kb(booked, date_iso),
        )
        await callback.answer(
            "Увы, это время только что заняли. Выберите другое.",
            show_alert=True,
        )
        return
    await state.update_data(time=time_str)
    await state.set_state(BookingForm.name)
    await safe_edit(
        callback,
        "Как вас зовут? Введите имя (например: Анна):",
        back_only_kb(),
    )
    await callback.answer()


@dp.message(StateFilter(BookingForm.name), F.text)
async def msg_name(message: Message, state: FSMContext) -> None:
    """Шаг 4: введено имя → просим телефон."""
    name = validate_name(message.text)
    if name is None:
        await message.answer(
            "Похоже, это не имя 🙂\n"
            "Введите имя текстом: 2–50 символов, только буквы. Например: Анна."
        )
        return
    await state.update_data(name=name)
    await state.set_state(BookingForm.phone)
    await message.answer(
        "Теперь введите номер телефона (например: +7 999 123-45-67):"
    )


@dp.message(StateFilter(BookingForm.phone), F.text)
async def msg_phone(message: Message, state: FSMContext) -> None:
    """Шаг 5: введён телефон → показываем сводку для подтверждения."""
    phone = validate_phone(message.text)
    if phone is None:
        await message.answer(
            "Не могу распознать номер.\n"
            "Подойдут форматы: +7 999 123-45-67, 89991234567, 9991234567.\n"
            "Нужно минимум 10 цифр."
        )
        return
    await state.update_data(phone=phone)
    await state.set_state(BookingForm.confirm)
    data = await state.get_data()
    await message.answer(booking_summary_text(data), reply_markup=confirm_kb())


@dp.callback_query(StateFilter(BookingForm.confirm), F.data == "confirm:yes")
async def cb_confirm_yes(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 6: запись подтверждена → сохраняем, уведомляем админа."""
    data = await state.get_data()
    service = SERVICES[data["service_id"]]
    # Финальная проверка слота перед записью в базу.
    if await is_slot_taken(data["date"], data["time"]):
        await state.clear()
        await safe_edit(
            callback,
            "К сожалению, это время только что заняли.\n"
            "Нажмите «📅 Записаться», чтобы выбрать другое.",
            main_menu_kb(),
        )
        await callback.answer()
        return
    booking_id = await create_booking(
        telegram_user_id=callback.from_user.id,
        name=data["name"],
        phone=data["phone"],
        service_id=data["service_id"],
        service_name=service["name"],
        date_iso=data["date"],
        time_str=data["time"],
    )
    await state.clear()
    await safe_edit(
        callback,
        "✅ <b>Вы записаны!</b>\n\n"
        f"💼 {service['name']} — {service['price']} ₽\n"
        f"📅 {human_date(data['date'])} в {data['time']}\n\n"
        f"Номер вашей записи: №{booking_id}.\n"
        "Ждём вас!",
        main_menu_kb(),
    )
    await callback.answer()
    await notify_admin(
        "<b>🔔 Новая запись!</b>\n\n"
        f"💼 Услуга: {service['name']} — {service['price']} ₽\n"
        f"📅 Дата: {human_date(data['date'])}\n"
        f"⏰ Время: {data['time']}\n"
        f"👤 Клиент: {html.escape(data['name'])}\n"
        f"📞 Телефон: {html.escape(data['phone'])}\n"
        f"🆔 Запись №{booking_id}"
    )


@dp.callback_query(StateFilter(BookingForm.confirm), F.data == "confirm:no")
async def cb_confirm_no(callback: CallbackQuery, state: FSMContext) -> None:
    """Клиент отменил запись на шаге подтверждения."""
    await state.clear()
    await safe_edit(
        callback,
        "Запись отменена. Если передумаете — я тут 🙂",
        main_menu_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel:last")
async def cb_cancel_last(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена последней активной записи из меню."""
    await state.clear()
    row = await cancel_last_active_booking(callback.from_user.id)
    if row is None:
        await safe_edit(
            callback,
            "Активных записей не найдено — возможно, запись уже отменена.",
            main_menu_kb(),
        )
    else:
        await safe_edit(
            callback,
            f"❌ Запись отменена: {row['service_name']}, "
            f"{human_date(row['date'])} в {row['time']}.",
            main_menu_kb(),
        )
        await notify_admin(
            "<b>⚠️ Клиент отменил запись</b>\n\n"
            f"💼 Услуга: {row['service_name']}\n"
            f"📅 Дата: {human_date(row['date'])}\n"
            f"⏰ Время: {row['time']}\n"
            f"👤 Клиент: {html.escape(row['name'])}\n"
            f"📞 Телефон: {html.escape(row['phone'])}"
        )
    await callback.answer()


# ============================================================
# 10. ОБРАБОТКА НЕПРЕДУСМОТРЕННОГО ВВОДА
# ============================================================

@dp.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    """Неизвестная команда."""
    await message.answer("Не знаю такую команду. Открыть меню: /start")


@dp.message()
async def fallback_message(message: Message) -> None:
    """Любое сообщение вне сценария записи."""
    await message.answer(
        "Я понимаю команды и кнопки 🙂\nОткрыть меню: /start"
    )


@dp.callback_query()
async def fallback_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Устаревшая кнопка (пользователь нажал кнопку из старого шага)."""
    await state.clear()
    await callback.answer(
        "Эта кнопка устарела. Начните заново: /start", show_alert=True
    )


@dp.error()
async def on_error(event) -> None:
    """Глобальный обработчик ошибок: логируем и не роняем бота."""
    logging.exception("Необработанная ошибка: %s", event.exception)


# ============================================================
# 11. ЗАПУСК
# ============================================================

async def main() -> None:
    await init_db()
    logging.info(
        "Бот запущен. ADMIN_CHAT_ID: %s",
        ADMIN_CHAT_ID if ADMIN_CHAT_ID else "не задан (уведомления — в консоль)",
    )
    # На случай, если раньше был включён webhook — снимаем его.
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
