import asyncio
import os
import sqlite3
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

MASTER_ID = int(os.getenv("MASTER_ID", "0"))
DB_PATH = "bookings.db"
REMIND_BEFORE = timedelta(hours=2)  # за сколько напоминать

SERVICES = {
    "haircut": "Стрижка — 1000 ₽",
    "beard": "Борода — 500 ₽",
    "combo": "Стрижка + борода — 1400 ₽",
}

TIMES = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]


# ---------- База данных ----------

def db_execute(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(query, params)
        conn.commit()
        return cur.fetchall()
    finally:
        conn.close()


def init_db():
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            service TEXT NOT NULL,
            day TEXT NOT NULL,
            time TEXT NOT NULL,
            UNIQUE(day, time)
        )
        """
    )
    # Добавляем колонку для напоминаний в уже существующую базу
    try:
        db_execute("ALTER TABLE bookings ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # колонка уже есть


def free_times(day):
    booked = {row[0] for row in db_execute("SELECT time FROM bookings WHERE day = ?", (day,))}
    now = datetime.now()
    result = []
    for t in TIMES:
        if t in booked:
            continue
        if day == now.date().isoformat() and t <= now.strftime("%H:%M"):
            continue
        result.append(t)
    return result


def pretty_date(day):
    return date.fromisoformat(day).strftime("%d.%m.%Y")


# ---------- Клавиатуры ----------

def services_keyboard():
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"service|{key}")]
        for key, title in SERVICES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_keyboard(service_key):
    today = date.today()
    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        buttons.append(
            InlineKeyboardButton(
                text=d.strftime("%d.%m"),
                callback_data=f"date|{service_key}|{d.isoformat()}",
            )
        )
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="back|services")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def times_keyboard(service_key, day):
    buttons = [
        InlineKeyboardButton(text=t, callback_data=f"time|{service_key}|{day}|{t}")
        for t in free_times(day)
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=f"service|{service_key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Команды ----------

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("Привет! Выберите услугу:", reply_markup=services_keyboard())


@dp.message(Command("my"))
async def my_bookings(message: Message):
    now = datetime.now()
    rows = db_execute(
        "SELECT id, day, time, service FROM bookings WHERE user_id = ? AND day >= ? ORDER BY day, time",
        (message.from_user.id, now.date().isoformat()),
    )
    # Убираем записи, время которых сегодня уже прошло
    upcoming = [r for r in rows if datetime.fromisoformat(f"{r[1]}T{r[2]}") > now]
    if not upcoming:
        await message.answer("У вас нет предстоящих записей. Чтобы записаться, отправьте /start")
        return
    for booking_id, day, time, service in upcoming:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cancel|{booking_id}")]
            ]
        )
        await message.answer(
            f"📅 {pretty_date(day)} в {time}\n{SERVICES[service]}",
            reply_markup=keyboard,
        )


@dp.message(Command("list"))
async def list_bookings(message: Message):
    if message.from_user.id != MASTER_ID:
        return
    rows = db_execute(
        "SELECT day, time, service, user_name FROM bookings WHERE day >= ? ORDER BY day, time",
        (date.today().isoformat(),),
    )
    if not rows:
        await message.answer("Ближайших записей нет.")
        return
    lines = []
    for day, time, service, name in rows:
        short = date.fromisoformat(day).strftime("%d.%m")
        lines.append(f"{short} {time} — {name} ({SERVICES[service]})")
    await message.answer("Ближайшие записи:\n\n" + "\n".join(lines))


# ---------- Кнопки ----------

@dp.callback_query(F.data == "back|services")
async def back_to_services(callback: CallbackQuery):
    await callback.message.edit_text("Выберите услугу:", reply_markup=services_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("service|"))
async def choose_service(callback: CallbackQuery):
    _, key = callback.data.split("|")
    await callback.message.edit_text(
        f"{SERVICES[key]}\n\nВыберите дату:", reply_markup=dates_keyboard(key)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("date|"))
async def choose_date(callback: CallbackQuery):
    _, key, day = callback.data.split("|")
    if not free_times(day):
        await callback.answer("На этот день свободного времени нет", show_alert=True)
        return
    await callback.message.edit_text(
        f"{SERVICES[key]}\nДата: {pretty_date(day)}\n\nВыберите время:",
        reply_markup=times_keyboard(key, day),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("time|"))
async def choose_time(callback: CallbackQuery):
    _, key, day, time = callback.data.split("|")
    user = callback.from_user
    try:
        db_execute(
            "INSERT INTO bookings (user_id, user_name, service, day, time) VALUES (?, ?, ?, ?, ?)",
            (user.id, user.full_name, key, day, time),
        )
    except sqlite3.IntegrityError:
        await callback.answer("Это время только что заняли. Выберите другое.", show_alert=True)
        await callback.message.edit_text(
            f"{SERVICES[key]}\nДата: {pretty_date(day)}\n\nВыберите время:",
            reply_markup=times_keyboard(key, day),
        )
        return

    await callback.message.edit_text(
        "✅ Вы записаны!\n\n"
        f"Услуга: {SERVICES[key]}\n"
        f"Дата: {pretty_date(day)}\n"
        f"Время: {time}\n\n"
        "Посмотреть или отменить запись: /my"
    )
    await callback.answer()

    if MASTER_ID:
        contact = f"@{user.username}" if user.username else "без username"
        await bot.send_message(
            MASTER_ID,
            "🔔 Новая запись!\n\n"
            f"Клиент: {user.full_name} ({contact})\n"
            f"Услуга: {SERVICES[key]}\n"
            f"Дата: {pretty_date(day)}\n"
            f"Время: {time}",
        )


@dp.callback_query(F.data.startswith("cancel|"))
async def cancel_booking(callback: CallbackQuery):
    _, booking_id = callback.data.split("|")
    user = callback.from_user
    # Условие user_id гарантирует, что клиент отменяет только свою запись
    rows = db_execute(
        "SELECT day, time, service FROM bookings WHERE id = ? AND user_id = ?",
        (booking_id, user.id),
    )
    if not rows:
        await callback.answer("Запись не найдена или уже отменена", show_alert=True)
        return
    day, time, service = rows[0]
    db_execute("DELETE FROM bookings WHERE id = ? AND user_id = ?", (booking_id, user.id))

    await callback.message.edit_text(f"❌ Запись отменена: {pretty_date(day)} в {time}")
    await callback.answer()

    if MASTER_ID:
        await bot.send_message(
            MASTER_ID,
            "❌ Клиент отменил запись\n\n"
            f"Клиент: {user.full_name}\n"
            f"Услуга: {SERVICES[service]}\n"
            f"Дата: {pretty_date(day)}\n"
            f"Время: {time}",
        )


# ---------- Напоминания ----------

async def reminder_loop():
    # Раз в минуту проверяем, кому пора отправить напоминание
    while True:
        try:
            now = datetime.now()
            rows = db_execute(
                "SELECT id, user_id, service, day, time FROM bookings WHERE reminded = 0 AND day >= ?",
                (now.date().isoformat(),),
            )
            for booking_id, user_id, service, day, time in rows:
                start = datetime.fromisoformat(f"{day}T{time}")
                if timedelta(0) < start - now <= REMIND_BEFORE:
                    db_execute("UPDATE bookings SET reminded = 1 WHERE id = ?", (booking_id,))
                    try:
                        await bot.send_message(
                            user_id,
                            f"⏰ Напоминание: сегодня в {time} у вас запись.\n{SERVICES[service]}",
                        )
                    except Exception as e:
                        print("Не удалось отправить напоминание:", e)
        except Exception as e:
            print("Ошибка в напоминаниях:", e)
        await asyncio.sleep(60)


async def main():
    init_db()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Записаться"),
            BotCommand(command="my", description="Мои записи"),
        ]
    )
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())