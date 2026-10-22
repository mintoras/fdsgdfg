import asyncio
import logging
import random
import string
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
import aiosqlite

# ============ CONFIG ============
BOT_TOKEN = "PUT_YOUR_TOKEN_HERE"        # <-- токен от BotFather
ADMIN_PASSWORD = "13290313"
CHANNEL_URL = "https://t.me/car_renter"
SUPPORT_URL = "https://t.me/arenterse"
PRICE_PER_MINUTE = 27
DB_PATH = "bot.db"                        # на Railway с volume -> "/data/bot.db"
SUPER_ADMIN_ID = 0                        # <-- твой user_id (получи через @userinfobot), 0 = авто-назначить первого вошедшего по паролю
# =================================

logging.basicConfig(level=logging.INFO)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ============ DB ============
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            is_banned INTEGER DEFAULT 0,
            ban_until TEXT,
            has_access INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS keys(
            key TEXT PRIMARY KEY,
            max_activations INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS key_users(
            key TEXT, user_id INTEGER,
            PRIMARY KEY(key, user_id)
        );
        CREATE TABLE IF NOT EXISTS rentals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            service TEXT,
            plate TEXT,
            started_at TEXT,
            ended_at TEXT,
            minutes INTEGER
        );
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP,
            level TEXT, user_id INTEGER,
            action TEXT, details TEXT
        );
        CREATE TABLE IF NOT EXISTS settings(
            name TEXT PRIMARY KEY, value TEXT
        );
        INSERT OR IGNORE INTO settings(name,value) VALUES('maintenance','0');
        """)
        await db.commit()

async def db_q(sql, args=(), fetch=False, many=False):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        if fetch:
            res = await cur.fetchall()
            await db.commit()
            return res
        await db.commit()
        return cur.lastrowid

async def log(level, user_id, action, details=""):
    await db_q("INSERT INTO logs(level,user_id,action,details) VALUES(?,?,?,?)",
              (level, user_id, action, details))

async def get_setting(name):
    r = await db_q("SELECT value FROM settings WHERE name=?", (name,), fetch=True)
    return r[0][0] if r else None

async def set_setting(name, value):
    await db_q("INSERT OR REPLACE INTO settings(name,value) VALUES(?,?)", (name, str(value)))

async def upsert_user(u):
    await db_q(
        "INSERT INTO users(user_id,username,full_name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
        (u.id, u.username, u.full_name)
    )

async def get_user(uid):
    r = await db_q("SELECT * FROM users WHERE user_id=?", (uid,), fetch=True)
    return r[0] if r else None

async def is_admin(uid):
    r = await db_q("SELECT is_banned FROM admins WHERE user_id=?", (uid,), fetch=True)
    return bool(r) and r[0][0] == 0

async def is_super(uid):
    if uid == SUPER_ADMIN_ID and SUPER_ADMIN_ID != 0:
        return True
    return False

async def key_activate(key, uid):
    r = await db_q("SELECT max_activations,used_count,is_active FROM keys WHERE key=?", (key,), fetch=True)
    if not r: return "not_found"
    maxa, used, active = r[0]
    if not active: return "inactive"
    if used >= maxa: return "limit"
    r2 = await db_q("SELECT 1 FROM key_users WHERE key=? AND user_id=?", (key, uid), fetch=True)
    if r2: return "already"
    await db_q("INSERT INTO key_users(key,user_id) VALUES(?,?)", (key, uid))
    await db_q("UPDATE keys SET used_count=used_count+1 WHERE key=?", (key,))
    await db_q("UPDATE users SET has_access=1 WHERE user_id=?", (uid,))
    return "ok"

# ============ FSM ============
class UserFlow(StatesGroup):
    wait_key = State()
    wait_plate = State()

class AdminFlow(StatesGroup):
    wait_password = State()
    key_make = State()
    key_max = State()
    admin_add = State()
    broadcast_text = State()
    broadcast_btn = State()
    user_msg_id = State()
    user_msg_text = State()
    ban_id = State()
    ban_minutes = State()

# ============ KB ============
def kb_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Готово", callback_data="check_sub")],
    ])

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Выбор автомобиля", callback_data="choose_car")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)],
    ])

def kb_services():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="YandexDrive", callback_data="svc_yandex")],
        [InlineKeyboardButton(text="Delimobile", callback_data="svc_deli")],
        [InlineKeyboardButton(text="CityDrive", callback_data="svc_city")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])

def kb_car():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Разблокировать автомобиль", callback_data="unlock")],
        [InlineKeyboardButton(text="🌐 Отключить геозоны", callback_data="no_geo")],
        [InlineKeyboardButton(text="📡 Отключить GPS", callback_data="no_gps")],
        [InlineKeyboardButton(text="⏹ Завершить аренду", callback_data="end_rental")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)],
    ])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Создать ключ", callback_data="a_key_create")],
        [InlineKeyboardButton(text="📋 Список ключей", callback_data="a_keys")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="a_users")],
        [InlineKeyboardButton(text="🛡 Админы", callback_data="a_admins")],
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="a_admin_add")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="a_broadcast")],
        [InlineKeyboardButton(text="🛠 Тех.перерыв", callback_data="a_maint")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="a_logs")],
    ])

def gen_key():
    return "CR-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=12))

# ============ MIDDLEWARE ============
@dp.message.middleware()
async def ban_middleware(handler, event: Message, data):
    u = event.from_user
    if not u: return await handler(event, data)
    row = await get_user(u.id)
    if row and row[4] == 1:
        if row[5]:
            if datetime.fromisoformat(row[5]) > datetime.utcnow():
                await event.answer("⛔ Вы забанены.")
                return
            else:
                await db_q("UPDATE users SET is_banned=0, ban_until=NULL WHERE user_id=?", (u.id,))
        else:
            await event.answer("⛔ Вы забанены навсегда.")
            return
    return await handler(event, data)

# ============ START / USER FLOW ============
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await upsert_user(msg.from_user)
    await log("INFO", msg.from_user.id, "start", "")
    maint = await get_setting("maintenance")
    if maint == "1" and not await is_admin(msg.from_user.id):
        await msg.answer("🛠 Бот на техническом перерыве. Загляни позже.")
        return
    await msg.answer(
        "👋 Привет!\n\nЧтобы продолжить, подпишись на наш канал и нажми <b>Готово</b>.",
        reply_markup=kb_start()
    )

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cq: CallbackQuery, state: FSMContext):
    await cq.message.edit_text("🔑 Введите секретный ключ, который выдал менеджер:")
    await state.set_state(UserFlow.wait_key)
    await cq.answer()

@dp.message(UserFlow.wait_key)
async def on_key(msg: Message, state: FSMContext):
    key = msg.text.strip()
    res = await key_activate(key, msg.from_user.id)
    if res == "ok":
        await state.clear()
        await log("INFO", msg.from_user.id, "key_ok", key)
        await msg.answer("✅ Ключ активирован. Добро пожаловать!", reply_markup=kb_main())
    else:
        errors = {
            "not_found": "❌ Ключ не найден.",
            "inactive": "❌ Ключ деактивирован.",
            "limit": "❌ Лимит активаций исчерпан.",
            "already": "⚠️ Вы уже активировали этот ключ.",
        }
        await msg.answer(errors.get(res, "❌ Ошибка."))

@dp.callback_query(F.data == "choose_car")
async def cb_choose_car(cq: CallbackQuery):
    await cq.message.edit_text("🚗 Выберите сервис:", reply_markup=kb_services())
    await cq.answer()

@dp.callback_query(F.data == "back_main")
async def cb_back_main(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text("Главное меню:", reply_markup=kb_main())
    await cq.answer()

@dp.callback_query(F.data.startswith("svc_"))
async def cb_svc(cq: CallbackQuery, state: FSMContext):
    svc = {"svc_yandex": "YandexDrive", "svc_deli": "Delimobile", "svc_city": "CityDrive"}[cq.data]
    await state.update_data(service=svc)
    await state.set_state(UserFlow.wait_plate)
    await cq.message.edit_text(
        f"🚙 Сервис: <b>{svc}</b>\n\nВведите номер автомобиля в формате <code>А123ВС77</code>:"
    )
    await cq.answer()

@dp.message(UserFlow.wait_plate)
async def on_plate(msg: Message, state: FSMContext):
    plate = msg.text.strip().upper().replace(" ", "")
    if len(plate) < 6 or not any(c.isdigit() for c in plate):
        await msg.answer("❌ Неверный формат. Пример: <code>А123ВС77</code>")
        return
    data = await state.get_data()
    svc = data.get("service", "Unknown")
    rental_id = await db_q(
        "INSERT INTO rentals(user_id,service,plate,started_at) VALUES(?,?,?,?)",
        (msg.from_user.id, svc, plate, datetime.utcnow().isoformat())
    )
    await state.clear()
    await state.update_data(rental_id=rental_id, plate=plate, service=svc)
    await log("INFO", msg.from_user.id, "rental_start", f"{svc} {plate}")
    await msg.answer(
        f"✅ Автомобиль <b>{plate}</b> ({svc}) подключён к системе.\n\nВыберите действие:",
        reply_markup=kb_car()
    )

@dp.callback_query(F.data.in_({"unlock", "no_geo", "no_gps"}))
async def cb_car_action(cq: CallbackQuery):
    actions = {
        "unlock": "🔓 Автомобиль разблокирован. Можно начинать поездку.",
        "no_geo": "🌐 Геозоны отключены.",
        "no_gps": "📡 GPS-трекинг отключён.",
    }
    await cq.answer(actions[cq.data], show_alert=True)
    await log("INFO", cq.from_user.id, "car_action", cq.data)

@dp.callback_query(F.data == "end_rental")
async def cb_end_rental(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    rid = data.get("rental_id")
    if not rid:
        await cq.answer("Аренда не найдена.", show_alert=True)
        return
    r = await db_q("SELECT started_at FROM rentals WHERE id=?", (rid,), fetch=True)
    if not r:
        await cq.answer("Аренда не найдена.", show_alert=True)
        return
    started = datetime.fromisoformat(r[0][0])
    minutes = max(1, int((datetime.utcnow() - started).total_seconds() // 60))
    cost = minutes * PRICE_PER_MINUTE
    await db_q("UPDATE rentals SET ended_at=?, minutes=? WHERE id=?",
               (datetime.utcnow().isoformat(), minutes, rid))
    await state.clear()
    await log("INFO", cq.from_user.id, "rental_end", f"min={minutes} cost={cost}")
    await cq.message.edit_text(
        f"⏹ Аренда завершена.\n\n"
        f"⏱ Время поездки: <b>{minutes} мин</b>\n"
        f"💰 Списано с системного аккаунта: <b>{cost} ₽</b>\n\n"
        f"Тариф: {PRICE_PER_MINUTE} ₽/мин",
        reply_markup=kb_main()
    )
    await cq.answer()

# ============ ADMIN ============
@dp.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    if await is_admin(msg.from_user.id):
        await msg.answer("🛡 Админ-панель:", reply_markup=kb_admin())
        return
    await msg.answer("Введите пароль администратора:")
    await state.set_state(AdminFlow.wait_password)

@dp.message(AdminFlow.wait_password)
async def admin_pass(msg: Message, state: FSMContext):
    if msg.text.strip() != ADMIN_PASSWORD:
        await msg.answer("❌ Неверный пароль.")
        await state.clear()
        return
    global SUPER_ADMIN_ID
    await db_q("INSERT OR IGNORE INTO admins(user_id,username) VALUES(?,?)",
               (msg.from_user.id, msg.from_user.username))
    if SUPER_ADMIN_ID == 0:
        SUPER_ADMIN_ID = msg.from_user.id
    await state.clear()
    await log("ADMIN", msg.from_user.id, "admin_login", "")
    await msg.answer("✅ Добро пожаловать в админ-панель:", reply_markup=kb_admin())

@dp.callback_query(F.data == "a_keys")
async def a_keys(cq: CallbackQuery):
    if not await is_admin(cq.from_user.id): return await cq.answer("Нет доступа", show_alert=True)
    rows = await db_q("SELECT key,max_activations,used_count,is_active FROM keys ORDER BY created_at DESC", fetch=True)
    if not rows:
        await cq.message.edit_text("Ключей нет.", reply_markup=kb_admin()); return await cq.answer()
    text = "📋 <b>Ключи:</b>\n\n"
    for k, maxa, used, active in rows[:30]:
        text += f"<code>{k}</code> — {used}/{maxa} {'✅' if active else '❌'}\n"
    await cq.message.edit_text(text, reply_markup=kb_admin())
    await cq.answer()

@dp.callback_query(F.data == "a_key_create")
async def a_key_create(cq: CallbackQuery, state: FSMContext):
    if not await is_admin(cq.from_user.id): return await cq.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFlow.key_make)
    await cq.message.edit_text("Введите название/префикс ключа (или отправьте <code>-</code> для автогенерации):")
    await cq.answer()

@dp.message(AdminFlow.key_make)
async def a_key_make(msg: Message, state: FSMContext):
    val = msg.text.strip()
    key = gen_key() if val == "-" else val
    await state.update_data(key=key)
    await state.set_state(AdminFlow.key_max)
    await msg.answer(f"Ключ: <code>{key}</code>\nСколько активаций максимум?")

@dp.message(AdminFlow.key_max)
async def a_key_max(msg: Message, state: FSMContext):
    try:
        n = int(msg.text.strip())
    except ValueError:
        await msg.answer("Введите число."); return
    data = await state.get_data()
    key = data["key"]
    await db_q("INSERT OR IGNORE INTO keys(key,max_activations,created_by) VALUES(?,?,?)",
               (key, n, msg.from_user.id))
    await state.clear()
    await log("ADMIN", msg.from_user.id, "key_create", f"{key} x{n}")
    await msg.answer(f"✅ Ключ создан: <code>{key}</code> (макс. {n})", reply_markup=kb_admin())

@dp.callback_query(F.data == "a_users")
async def a_users(cq: CallbackQuery):
    if not await is_admin(cq.from_user.id): return await cq.answer("Нет доступа", show_alert=True)
    rows = await db_q("SELECT user_id,username,full_name,is_banned FROM users LIMIT 50", fetch=True)
    text = "👥 <b>Пользователи (до 50):</b>\n\n"
    for uid, un, fn, banned in rows:
        text += f"<code>{uid}</code> @{un or '-'} {fn or ''} {'⛔' if banned else ''}\n"
    text += "\nЧтобы написать — /dm ID текст\nЧтобы забанить — /ban ID [минуты]\nРазбан — /unban ID"
    await cq.message.edit_text(text[:4000], reply_markup=kb_admin())
    await cq.answer()

@dp.message(Command("dm"))
async def cmd_dm(msg: Message):
    if not await is_admin(msg.from_user.id): return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.answer("Формат: /dm ID текст"); return
    try:
        uid = int(parts[1])
        await bot.send_message(uid, f"📩 Сообщение от администрации:\n\n{parts[2]}")
        await msg.answer("✅ Отправлено.")
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")

@dp.message(Command("ban"))
async def cmd_ban(msg: Message):
    if not await is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 2: return await msg.answer("Формат: /ban ID [минуты]")
    try: uid = int(parts[1])
    except: return await msg.answer("ID — число")
    until = None
    if len(parts) >= 3:
        until = (datetime.utcnow() + timedelta(minutes=int(parts[2]))).isoformat()
    await db_q("UPDATE users SET is_banned=1, ban_until=? WHERE user_id=?", (until, uid))
    await log("ADMIN", msg.from_user.id, "ban", f"{uid} until={until}")
    await msg.answer(f"⛔ Пользователь {uid} забанен.")

@dp.message(Command("unban"))
async def cmd_unban(msg: Message):
    if not await is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 2: return await msg.answer("Формат: /unban ID")
    try: uid = int(parts[1])
    except: return await msg.answer("ID — число")
    await db_q("UPDATE users SET is_banned=0, ban_until=NULL WHERE user_id=?", (uid,))
    await msg.answer(f"✅ Пользователь {uid} разбанен.")

@dp.callback_query(F.data == "a_admins")
async def a_admins(cq: CallbackQuery):
    if not await is_admin(cq.from_user.id): return await cq.answer("Нет доступа", show_alert=True)
    rows = await db_q("SELECT user_id,username,is_banned FROM admins", fetch=True)
    text = "🛡 <b>Админы:</b>\n\n"
    for uid, un, banned in rows:
        text += f"<code>{uid}</code> @{un or '-'} {'⛔' if banned else ''}\n"
    text += "\n/banadmin ID — забан\n/unbanadmin ID — разбан\n/removeadmin ID — снять"
    await cq.message.edit_text(text, reply_markup=kb_admin())
    await cq.answer()

@dp.callback_query(F.data == "a_admin_add")
async def a_admin_add(cq: CallbackQuery, state: FSMContext):
    if not await is_admin(cq.from_user.id): return await cq.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFlow.admin_add)
    await cq.message.edit_text("Введите user_id нового админа:")
    await cq.answer()

@dp.message(AdminFlow.admin_add)
async def a_admin_add_done(msg: Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: return await msg.answer("ID — число")
    await db_q("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
    await state.clear()
    await log("ADMIN", msg.from_user.id, "admin_add", str(uid))
    await msg.answer(f"✅ Админ {uid} добавлен.", reply_markup=kb_admin())

@dp.message(Command("banadmin"))
async def cmd_banadmin(msg: Message):
    if not await is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 2: return await msg.answer("Формат: /banadmin ID")
    await db_q("UPDATE admins SET is_banned=1 WHERE user_id=?", (int(parts[1]),))
    await msg.answer("⛔ Админ забанен.")

@dp.message(Command("unbanadmin"))
async def cmd_unbanadmin(msg: Message):
    if not await is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 2: return await msg.answer("Формат: /unbanadmin ID")
    await db_q("UPDATE admins SET is_banned=0 WHERE user_id=?", (int(parts[1]),))
    await msg.answer("✅ Разбанен.")

@dp.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message):
    if not await is_admin(msg.from_user.id): return
    parts = msg.text.split()
    if len(parts) < 2: return await msg.answer("Формат: /removeadmin ID")
    await db_q("DELETE FROM admins WHERE user_id=?", (int(parts[1]),))
    await msg.answer("🗑 Снят.")

@dp.callback_query(F.data == "a_broadcast")
async def a_broadcast(cq: CallbackQuery, state: FSMContext):
    if not await is_admin(cq.from_user.id): return await cq.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFlow.broadcast_text)
    await cq.message.edit_text("Введите текст рассылки (можно с HTML).\nИли отправьте <code>-</code> чтобы пропустить текст и сразу перейти к кнопке.")
    await cq.answer()

@dp.message(AdminFlow.broadcast_text)
async def a_broadcast_text(msg: Message, state: FSMContext):
    await state.update_data(btext=msg.text)
    await state.set_state(AdminFlow.broadcast_btn)
    await msg.answer("Теперь кнопка в формате: <code>Текст | https://url</code>\nИли <code>-</code> без кнопки.")

@dp.message(AdminFlow.broadcast_btn)
async def a_broadcast_go(msg: Message, state: FSMContext):
    data = await state.get_data()
    btext = data.get("btext", "-")
    bbtn = msg.text.strip()
    markup = None
    if bbtn != "-" and "|" in bbtn:
        t, u = [x.strip() for x in bbtn.split("|", 1)]
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, url=u)]])
    if btext == "-": btext = None
    rows = await db_q("SELECT user_id FROM users WHERE is_banned=0", fetch=True)
    ok = fail = 0
    for (uid,) in rows:
        try:
            if btext and markup:
                await bot.send_message(uid, btext, reply_markup=markup)
            elif btext:
                await bot.send_message(uid, btext)
            elif markup:
                await bot.send_message(uid, "📢", reply_markup=markup)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await state.clear()
    await log("ADMIN", msg.from_user.id, "broadcast", f"ok={ok} fail={fail}")
    await msg.answer(f"✅ Рассылка завершена. Успешно: {ok}, ошибок: {fail}", reply_markup=kb_admin())

@dp.callback_query(F.data == "a_maint")
async def a_maint(cq: CallbackQuery):
    if not await is_admin(cq.from_user.id): return await cq.answer("Нет доступа", show_alert=True)
    cur = await get_setting("maintenance")
    new = "0" if cur == "1" else "1"
    await set_setting("maintenance", new)
    await log("ADMIN", cq.from_user.id, "maint", new)
    state = "включён" if new == "1" else "выключен"
    await cq.answer(f"Тех.перерыв {state}", show_alert=True)

@dp.callback_query(F.data == "a_logs")
async def a_logs(cq: CallbackQuery):
    if not await is_admin(cq.from_user.id): return await cq.answer("Нет доступа", show_alert=True)
    rows = await db_q("SELECT ts,level,user_id,action,details FROM logs ORDER BY id DESC LIMIT 30", fetch=True)
    text = "📜 <b>Последние 30 логов:</b>\n\n"
    for ts, lvl, uid, act, det in rows:
        text += f"<code>{ts[:19]}</code> [{lvl}] {uid} {act} {det}\n"
    await cq.message.edit_text(text[:4000], reply_markup=kb_admin())
    await cq.answer()

# ============ RUN ============
async def main():
    await init_db()
    logging.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())