"""
Telegram channel post watcher + auto-comment bot
Single-file implementation: Aiogram 3 + Telethon + SQLite.

Install:
    pip install -U aiogram telethon

Run:
    python channel_comment_bot.py

Set BOT_TOKEN and ADMIN_ID in the КОНФИГ block at the top of this file.
API_ID / API_HASH are entered later through the bot menu (⚙️ API ID / HASH).
Use only with an account/channels where you are authorized to participate.
New comments are posted automatically to the linked discussion group.
"""

import asyncio
import logging
import random
import sqlite3
from contextlib import suppress
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.functions.messages import GetDiscussionMessageRequest


DB_PATH = "channel_comment_bot.sqlite3"
SESSION_PATH = "user_account"
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("channel-comment-bot")


# ============================ КОНФИГ ========================================
# Токен бота (получить у @BotFather) — впиши сюда:
BOT_TOKEN = "9172898093:AAHa7HRiwaBTFCGaZ_R7tMWR2lKIY2BEK2E"        # например: "123456789:AAExAmPlEtOkEn"

# Telegram ID главного администратора (узнать можно у @userinfobot):
ADMIN_ID = 6062360234         # например: 123456789
# ============================================================================


# ----------------------------- Database ------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS config
                     (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS channels
                     (channel TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS words
                     (word TEXT PRIMARY KEY)""")
        c.execute("""CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY, added_by INTEGER,
                      added_at TEXT DEFAULT CURRENT_TIMESTAMP)""")


def get_cfg(key, default=""):
    with db() as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_cfg(key, value):
    with db() as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


def list_channels():
    with db() as c:
        return c.execute("SELECT channel FROM channels WHERE enabled=1 ORDER BY channel").fetchall()


def add_channel(channel):
    channel = channel.strip()
    with db() as c:
        c.execute("INSERT OR REPLACE INTO channels(channel,enabled) VALUES(?,1)", (channel,))


def remove_channel(channel):
    with db() as c:
        c.execute("DELETE FROM channels WHERE channel=?", (channel.strip(),))


def add_words(raw):
    words = [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
    with db() as c:
        c.executemany("INSERT OR IGNORE INTO words(word) VALUES(?)", [(w,) for w in words])
    return len(words)


def list_words():
    with db() as c:
        return [r["word"] for r in c.execute("SELECT word FROM words ORDER BY word")]


def remove_word(word):
    with db() as c:
        c.execute("DELETE FROM words WHERE word=?", (word.strip(),))


# ----------------------------- FSM -----------------------------------------

class Setup(StatesGroup):
    bot_token = State()
    admin_id = State()
    api_id = State()
    api_hash = State()
    channel = State()
    remove_channel = State()
    words = State()
    remove_word = State()
    phone = State()
    code = State()
    password = State()
    add_admin = State()
    remove_admin = State()


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Подключить аккаунт", callback_data="connect")],
        [InlineKeyboardButton(text="📡 Каналы", callback_data="channels"),
         InlineKeyboardButton(text="📝 Слова", callback_data="words")],
        [InlineKeyboardButton(text="⚙️ API ID / HASH", callback_data="api")],
        [InlineKeyboardButton(text="👮 Администраторы", callback_data="admins")],
        [InlineKeyboardButton(text="▶️ Запустить мониторинг", callback_data="monitor")],
        [InlineKeyboardButton(text="⏹ Остановить мониторинг", callback_data="stop")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="status")]
    ])


def channels_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")],
        [InlineKeyboardButton(text="➖ Удалить канал", callback_data="del_channel")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])


def words_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить слова", callback_data="add_words")],
        [InlineKeyboardButton(text="➖ Удалить слово", callback_data="del_word")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])


# ----------------------------- Runtime -------------------------------------

bot: Optional[Bot] = None
dp = Dispatcher()
user_client: Optional[TelegramClient] = None
monitoring = False
client_lock = asyncio.Lock()
login_phone = None
login_code_hash = None


def is_admin(user_id):
    try:
        if user_id == int(get_cfg("admin_id", "0")):
            return True
    except (ValueError, TypeError):
        pass
    with db() as c:
        row = c.execute("SELECT 1 FROM admins WHERE user_id=?", (int(user_id),)).fetchone()
    return row is not None


def list_admins():
    owner = int(get_cfg("admin_id", "0") or 0)
    with db() as c:
        rows = c.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()
    ids = [int(r["user_id"]) for r in rows]
    if owner and owner not in ids:
        ids.insert(0, owner)
    return ids


def add_admin_id(user_id, added_by):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO admins(user_id, added_by) VALUES(?,?)",
                  (int(user_id), int(added_by)))


def remove_admin_id(user_id):
    # The primary ADMIN_ID is the owner and cannot be removed from the panel.
    if int(user_id) == int(get_cfg("admin_id", "0") or 0):
        return False
    with db() as c:
        cur = c.execute("DELETE FROM admins WHERE user_id=?", (int(user_id),))
    return cur.rowcount > 0


async def admin_only(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return False
    return True


async def cb_admin(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return False
    await call.answer()
    return True


async def start_user_client():
    global user_client
    async with client_lock:
        if user_client and user_client.is_connected():
            return True
        api_id = get_cfg("api_id")
        api_hash = get_cfg("api_hash")
        if not api_id or not api_hash:
            return False
        user_client = TelegramClient(SESSION_PATH, int(api_id), api_hash)
        await user_client.connect()
        if not await user_client.is_user_authorized():
            return False
        return True


async def disconnect_user_client():
    global user_client, monitoring
    monitoring = False
    if user_client:
        with suppress(Exception):
            await user_client.disconnect()
        user_client = None


async def resolve_channel(client, channel):
    channel = channel.strip()
    if channel.startswith("https://t.me/"):
        channel = channel.removeprefix("https://t.me/")
    if channel.startswith("http://t.me/"):
        channel = channel.removeprefix("http://t.me/")
    if channel.startswith("@"):
        channel = channel[1:]
    return await client.get_entity(channel)


async def post_to_discussion(channel_entity, post_id, comment):
    """Post immediately as a reply in the linked discussion group."""
    result = await user_client(GetDiscussionMessageRequest(
        peer=channel_entity, msg_id=post_id
    ))
    messages = getattr(result, "messages", [])
    chats = getattr(result, "chats", [])
    if not messages or not chats:
        raise RuntimeError("У канала нет доступной группы обсуждений.")
    discussion_root = messages[0]
    discussion_chat = chats[0]
    sent = await user_client.send_message(
        discussion_chat, comment, reply_to=discussion_root.id
    )
    return sent


async def begin_monitoring():
    global monitoring
    if not await start_user_client():
        raise RuntimeError("Сначала подключи аккаунт и настрой API ID / HASH.")
    if monitoring:
        return
    entities = []
    for row in list_channels():
        try:
            entities.append(await resolve_channel(user_client, row["channel"]))
        except Exception as e:
            log.warning("Не удалось открыть канал %s: %s", row["channel"], e)
    if not entities:
        raise RuntimeError("Нет доступных каналов. Проверь список каналов и подписку.")
    monitoring = True

    @user_client.on(events.NewMessage(chats=entities))
    async def new_post(event):
        if not monitoring or not event.is_channel:
            return
        # Ignore messages that are replies/comments rather than channel posts.
        if getattr(event.message, "post", None) is False:
            return
        words = list_words()
        if not words:
            return
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)
        channel_label = "@" + chat.username if getattr(chat, "username", None) else title
        comment = random.choice(words)
        # Post the comment immediately, no admin approval.
        try:
            await post_to_discussion(chat, event.id, comment)
            log.info("Комментарий отправлен: %s, пост %s", channel_label, event.id)
        except FloodWaitError as e:
            log.warning("FloodWait %s сек: %s, пост %s", e.seconds, channel_label, event.id)
        except Exception as e:
            log.error("Не удалось отправить комментарий (%s, пост %s): %s: %s",
                      channel_label, event.id, type(e).__name__, e)

    # Keep Telethon event loop alive; this task ends when disconnected.
    log.info("Monitoring enabled for %d channels", len(entities))


# ----------------------------- Bot handlers --------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Бот доступен только администратору.")
        return
    await message.answer(
        "Управление мониторингом каналов.\n"
        "Сначала укажи API ID и API HASH, затем подключи аккаунт.",
        reply_markup=main_kb()
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_kb())


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    if not await cb_admin(call): return
    await call.message.edit_text("Главное меню:", reply_markup=main_kb())


@dp.callback_query(F.data == "channels")
async def channels_menu(call: CallbackQuery):
    if not await cb_admin(call): return
    items = [r["channel"] for r in list_channels()]
    await call.message.edit_text(
        "📡 Каналы:\n" + ("\n".join("• " + x for x in items) if items else "Пока нет каналов."),
        reply_markup=channels_kb()
    )


@dp.callback_query(F.data == "words")
async def words_menu(call: CallbackQuery):
    if not await cb_admin(call): return
    items = list_words()
    await call.message.edit_text(
        "📝 Слова:\n" + ("\n".join("• " + x for x in items) if items else "Пока нет слов."),
        reply_markup=words_kb()
    )


@dp.callback_query(F.data == "add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    if not await cb_admin(call): return
    await state.set_state(Setup.channel)
    await call.message.answer("Отправь @username или ссылку t.me канала. Можно по одному на строку или через запятую.")


@dp.message(Setup.channel)
async def add_channel_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    raw = message.text or ""
    vals = [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
    for val in vals:
        add_channel(val)
    await state.clear()
    await message.answer(f"Добавлено каналов: {len(vals)}", reply_markup=main_kb())


@dp.callback_query(F.data == "del_channel")
async def del_channel_start(call: CallbackQuery, state: FSMContext):
    if not await cb_admin(call): return
    await state.set_state(Setup.remove_channel)
    await call.message.answer("Укажи канал для удаления (точно как добавлял).")


@dp.message(Setup.remove_channel)
async def del_channel_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    remove_channel(message.text or "")
    await state.clear()
    await message.answer("Канал удалён, если он был в списке.", reply_markup=main_kb())


@dp.callback_query(F.data == "add_words")
async def add_words_start(call: CallbackQuery, state: FSMContext):
    if not await cb_admin(call): return
    await state.set_state(Setup.words)
    await call.message.answer("Отправь слова через запятую или каждое с новой строки.")


@dp.message(Setup.words)
async def add_words_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    count = add_words(message.text or "")
    await state.clear()
    await message.answer(f"Добавлено слов: {count}", reply_markup=main_kb())


@dp.callback_query(F.data == "del_word")
async def del_word_start(call: CallbackQuery, state: FSMContext):
    if not await cb_admin(call): return
    await state.set_state(Setup.remove_word)
    await call.message.answer("Отправь слово, которое нужно удалить.")


@dp.message(Setup.remove_word)
async def del_word_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    remove_word(message.text or "")
    await state.clear()
    await message.answer("Слово удалено, если оно существовало.", reply_markup=main_kb())


@dp.callback_query(F.data == "admins")
async def admins_menu(call: CallbackQuery):
    if not await cb_admin(call): return
    ids = list_admins()
    text = "👮 Администраторы:\n" + ("\n".join(f"• <code>{uid}</code>" for uid in ids)
                                       if ids else "Список пуст.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить администратора", callback_data="add_admin")],
        [InlineKeyboardButton(text="➖ Удалить администратора", callback_data="remove_admin")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "add_admin")
async def add_admin_start(call: CallbackQuery, state: FSMContext):
    if not await cb_admin(call): return
    await state.set_state(Setup.add_admin)
    await call.message.answer("Отправь числовой Telegram ID нового администратора.\n"
                              "Узнать ID можно через @userinfobot.")


@dp.message(Setup.add_admin)
async def add_admin_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуй ещё раз или /cancel.")
        return
    uid = int(raw)
    if uid <= 0:
        await message.answer("Некорректный ID.")
        return
    add_admin_id(uid, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Пользователь <code>{uid}</code> добавлен в администраторы.",
                         parse_mode="HTML", reply_markup=main_kb())


@dp.callback_query(F.data == "remove_admin")
async def remove_admin_start(call: CallbackQuery, state: FSMContext):
    if not await cb_admin(call): return
    await state.set_state(Setup.remove_admin)
    await call.message.answer("Отправь Telegram ID администратора, которого нужно удалить.\n"
                              "Главного администратора удалить нельзя.")


@dp.message(Setup.remove_admin)
async def remove_admin_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуй ещё раз или /cancel.")
        return
    uid = int(raw)
    if uid == message.from_user.id:
        await message.answer("Нельзя удалить самого себя.")
        return
    removed = remove_admin_id(uid)
    await state.clear()
    await message.answer("✅ Администратор удалён." if removed else
                         "Такого администратора нет или это главный администратор.",
                         reply_markup=main_kb())


@dp.callback_query(F.data == "api")
async def api_start(call: CallbackQuery, state: FSMContext):
    if not await cb_admin(call): return
    await state.set_state(Setup.api_id)
    await call.message.answer("Отправь API ID (получить можно на my.telegram.org).")


@dp.message(Setup.api_id)
async def api_id_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    if not (message.text or "").isdigit():
        await message.answer("API ID должен быть числом. Попробуй ещё раз.")
        return
    set_cfg("api_id", message.text.strip())
    await state.set_state(Setup.api_hash)
    await message.answer("Теперь отправь API HASH.")


@dp.message(Setup.api_hash)
async def api_hash_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    set_cfg("api_hash", (message.text or "").strip())
    await state.clear()
    await message.answer("API ID и HASH сохранены.", reply_markup=main_kb())


@dp.callback_query(F.data == "connect")
async def connect_start(call: CallbackQuery, state: FSMContext):
    if not await cb_admin(call): return
    if not get_cfg("api_id") or not get_cfg("api_hash"):
        await call.message.answer("Сначала настрой API ID / HASH.")
        return
    await state.set_state(Setup.phone)
    await call.message.answer("Отправь номер телефона аккаунта в международном формате, например +49123456789.")


@dp.message(Setup.phone)
async def phone_finish(message: Message, state: FSMContext):
    global user_client, login_phone, login_code_hash
    if not await admin_only(message): return
    login_phone = (message.text or "").strip()
    try:
        user_client = TelegramClient(SESSION_PATH, int(get_cfg("api_id")), get_cfg("api_hash"))
        await user_client.connect()
        sent = await user_client.send_code_request(login_phone)
        login_code_hash = sent.phone_code_hash
        await state.set_state(Setup.code)
        await message.answer("Код отправлен в Telegram. Отправь код сюда. Не передавай его никому.")
    except Exception as e:
        await state.clear()
        await message.answer(f"Не удалось отправить код: {type(e).__name__}: {e}")


@dp.message(Setup.code)
async def code_finish(message: Message, state: FSMContext):
    global user_client
    if not await admin_only(message): return
    code = (message.text or "").strip().replace(" ", "")
    try:
        await user_client.sign_in(phone=login_phone, code=code, phone_code_hash=login_code_hash)
        await state.clear()
        me = await user_client.get_me()
        await message.answer(f"Аккаунт подключён: {me.first_name or ''} (@{me.username or 'без username'}).",
                             reply_markup=main_kb())
    except SessionPasswordNeededError:
        await state.set_state(Setup.password)
        await message.answer("Введи пароль двухэтапной аутентификации (2FA).")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await message.answer("Код неверный или истёк. Отправь актуальный код ещё раз.")
    except Exception as e:
        await state.clear()
        await message.answer(f"Ошибка входа: {type(e).__name__}: {e}")


@dp.message(Setup.password)
async def password_finish(message: Message, state: FSMContext):
    if not await admin_only(message): return
    try:
        await user_client.sign_in(password=(message.text or "").strip())
        me = await user_client.get_me()
        await state.clear()
        await message.answer(f"Аккаунт подключён: {me.first_name or ''}.", reply_markup=main_kb())
    except Exception as e:
        await message.answer(f"Не удалось войти: {type(e).__name__}: {e}")


@dp.callback_query(F.data == "monitor")
async def monitor_start(call: CallbackQuery):
    if not await cb_admin(call): return
    try:
        await begin_monitoring()
        await call.message.answer("✅ Мониторинг новых постов включён.")
    except Exception as e:
        await call.message.answer(f"Не удалось запустить: {e}")


@dp.callback_query(F.data == "stop")
async def monitor_stop(call: CallbackQuery):
    global monitoring
    if not await cb_admin(call): return
    monitoring = False
    await call.message.answer("⏸ Мониторинг остановлен.")


@dp.callback_query(F.data == "status")
async def status(call: CallbackQuery):
    if not await cb_admin(call): return
    connected = bool(user_client and user_client.is_connected() and await user_client.is_user_authorized())
    await call.message.answer(
        f"Аккаунт: {'подключён' if connected else 'не подключён'}\n"
        f"Мониторинг: {'работает' if monitoring else 'остановлен'}\n"
        f"Каналов: {len(list_channels())}\n"
        f"Слов: {len(list_words())}"
    )


async def main():
    global bot
    init_db()
    token = BOT_TOKEN.strip()
    if not token:
        print("Открой файл и впиши BOT_TOKEN в блоке КОНФИГ в самом начале файла.")
        return
    if not ADMIN_ID:
        print("Открой файл и впиши ADMIN_ID в блоке КОНФИГ в самом начале файла.")
        return
    # Файл — источник истины: при каждом запуске обновляем значения в БД.
    set_cfg("bot_token", token)
    set_cfg("admin_id", str(ADMIN_ID))
    bot = Bot(token=token)
    # Reconnect existing user session if already authorized.
    with suppress(Exception):
        await start_user_client()
    try:
        await dp.start_polling(bot)
    finally:
        await disconnect_user_client()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
