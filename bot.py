"""
Telegram-бот для канала и группы обсуждения.

Возможности:
  1. /start в личке — красивое приветствие пользователя.
  2. Приветствие новых участников в группе обсуждения канала
     (упоминание по имени/фамилии + авто-удаление через 10 секунд).
  3. Первый комментарий с правилами и rules.png под каждым постом канала.

Стек: Python 3.10+, aiogram 3.x
Запуск: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import struct
import zlib
from contextlib import suppress
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import ChatMemberUpdatedFilter, CommandStart, IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import (
    BufferedInputFile,
    ChatMemberUpdated,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)

import moderation
import stats
from server import start_web

# ──────────────────────────────────────────────────────────────
#  КОНФИГУРАЦИЯ
# ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")

# ID канала. Telegram отдаёт "короткий" id (4339622999) —
# в Bot API он используется в полном виде с префиксом -100.
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004339622999"))

# ID группы обсуждения канала (уже в полном формате).
DISCUSSION_CHAT_ID = int(os.getenv("DISCUSSION_CHAT_ID", "-1004332798085"))

# Публичная ссылка на канал (для кнопки в /start).
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/your_channel")

# Через сколько секунд удалять приветствие в группе.
GREETING_TTL = int(os.getenv("GREETING_TTL", "10"))

# Удалять ли системное сообщение «X присоединился к группе».
DELETE_SERVICE_MESSAGE = os.getenv("DELETE_SERVICE_MESSAGE", "1") == "1"

# Изображение и включение правил под публикациями канала.
RULES_ENABLED = os.getenv("RULES_ENABLED", "1") == "1"
RULES_IMAGE_PATH = Path(__file__).parent / Path(
    os.getenv("RULES_IMAGE", "rules.png")
).name

RULES_TEXT = (
    "<b>Правила обсуждения</b>\n\n"
    "1. Общайтесь уважительно, без оскорблений и провокаций.\n"
    "2. Обсуждайте публикацию и придерживайтесь темы.\n"
    "3. Не размещайте спам, рекламу и подозрительные ссылки.\n"
    "4. Не публикуйте чужие персональные данные.\n"
    "5. Запрещены незаконные материалы и любой вредоносный контент.\n\n"
    "Нарушения могут привести к удалению сообщений или блокировке."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("channel-bot")

router = Router(name="main")


# ──────────────────────────────────────────────────────────────
#  ИЗОБРАЖЕНИЕ ПРАВИЛ
# ──────────────────────────────────────────────────────────────

_generated_rules_png: bytes | None = None


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def build_default_rules_png() -> bytes:
    """Создаёт PNG-заглушку без внешних библиотек, если rules.png не загружен."""
    global _generated_rules_png
    if _generated_rules_png is not None:
        return _generated_rules_png

    width, height = 1200, 630
    pixels = bytearray(width * height * 3)

    # Тёмный градиент с двумя мягкими цветовыми акцентами.
    for y in range(height):
        for x in range(width):
            blue_glow = max(0.0, 1.0 - ((x - 980) ** 2 / 420000 + (y - 80) ** 2 / 150000))
            green_glow = max(0.0, 1.0 - ((x - 160) ** 2 / 360000 + (y - 600) ** 2 / 180000))
            grid = 5 if x % 60 == 0 or y % 60 == 0 else 0
            offset = (y * width + x) * 3
            pixels[offset] = min(255, 7 + int(20 * blue_glow) + grid)
            pixels[offset + 1] = min(255, 10 + int(40 * green_glow) + int(18 * blue_glow) + grid)
            pixels[offset + 2] = min(255, 18 + int(58 * blue_glow) + int(25 * green_glow) + grid)

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for py in range(max(0, y0), min(height, y1)):
            row = (py * width + max(0, x0)) * 3
            for _ in range(max(0, x0), min(width, x1)):
                pixels[row : row + 3] = bytes(color)
                row += 3

    glyphs = {
        "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
        "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
        "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
        "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
        "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
        "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
        "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    }

    def draw_text(text: str, x: int, y: int, scale: int) -> None:
        cursor = x
        for char in text:
            if char == " ":
                cursor += scale * 4
                continue
            for row, pattern in enumerate(glyphs[char]):
                for col, value in enumerate(pattern):
                    if value == "1":
                        rect(
                            cursor + col * scale,
                            y + row * scale,
                            cursor + (col + 1) * scale - 2,
                            y + (row + 1) * scale - 2,
                            (236, 242, 250),
                        )
            cursor += scale * 6

    title = "CHAT RULES"
    scale = 18
    title_width = (len(title.replace(" ", "")) * 6 + 4) * scale
    draw_text(title, (width - title_width) // 2, 190, scale)
    rect(310, 360, 890, 366, (52, 211, 153))
    rect(390, 410, 810, 418, (50, 80, 110))
    rect(450, 448, 750, 456, (38, 62, 87))

    raw = bytearray()
    row_size = width * 3
    for y in range(height):
        raw.append(0)
        start = y * row_size
        raw.extend(pixels[start : start + row_size])

    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    _generated_rules_png = (
        signature
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )
    return _generated_rules_png


def ensure_rules_image() -> None:
    """Создаёт rules.png в корне, но никогда не перезаписывает пользовательский файл."""
    if RULES_IMAGE_PATH.is_file() and RULES_IMAGE_PATH.stat().st_size > 0:
        return
    try:
        RULES_IMAGE_PATH.write_bytes(build_default_rules_png())
        log.info("Создано стандартное изображение %s", RULES_IMAGE_PATH.name)
    except OSError as error:
        # При read-only ФС изображение всё равно отправится напрямую из памяти.
        log.warning("Не удалось записать %s: %s", RULES_IMAGE_PATH.name, error)


def rules_photo() -> FSInputFile | BufferedInputFile:
    if RULES_IMAGE_PATH.is_file() and RULES_IMAGE_PATH.stat().st_size > 0:
        return FSInputFile(RULES_IMAGE_PATH, filename="rules.png")
    return BufferedInputFile(build_default_rules_png(), filename="rules.png")


# ──────────────────────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────────────────────

def html_escape(text: str) -> str:
    """Экранирование спецсимволов для HTML parse_mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def full_name(user: User) -> str:
    """Имя + фамилия пользователя (фамилия может отсутствовать)."""
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or "Друг"


def mention(user: User) -> str:
    """
    Кликабельное упоминание пользователя.
    Работает даже если у человека скрыт/отсутствует @username.
    """
    return f'<a href="tg://user?id={user.id}">{html_escape(full_name(user))}</a>'


def name_card(user: User) -> str:
    """Строки с именем, фамилией и @username."""
    first = html_escape(user.first_name or "—")
    last = html_escape(user.last_name or "—")
    uname = f"@{user.username}" if user.username else "не указан"
    return (
        f"├ <b>Имя:</b> {first}\n"
        f"├ <b>Фамилия:</b> {last}\n"
        f"└ <b>Username:</b> {html_escape(uname)}"
    )


GREETING_TEMPLATES = [
    "👋 {m}, добро пожаловать в чат обсуждения!",
    "🎉 {m} с нами! Рады видеть тебя в обсуждении.",
    "✨ Привет, {m}! Отличное пополнение в нашем чате.",
    "🚀 {m}, добро пожаловать на борт!",
    "🔥 Встречаем — {m} присоединился к обсуждению!",
]


async def delete_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    """Удаляет сообщение через `delay` секунд, молча игнорируя ошибки."""
    await asyncio.sleep(delay)
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        log.info("Приветствие %s удалено из чата %s", message_id, chat_id)


def schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: int = GREETING_TTL) -> None:
    """Планирует удаление сообщения в фоне (не блокирует обработчик)."""
    asyncio.create_task(delete_later(bot, chat_id, message_id, delay))


# ──────────────────────────────────────────────────────────────
#  1. ЛИЧКА: /start
# ──────────────────────────────────────────────────────────────

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    log.info("/start от %s (id=%s)", full_name(user), user.id)

    text = (
        f"👋 <b>Привет, {mention(user)}!</b>\n\n"
        f"Я — бот канала. Помогаю следить за обсуждениями "
        f"и встречаю новых участников.\n\n"
        f"<b>Твой профиль</b>\n{name_card(user)}\n\n"
        f"<b>Что я умею:</b>\n"
        f"• приветствовать новичков в чате обсуждения;\n"
        f"• автоматически удалять спам и выдавать предупреждения;\n"
        f"• принимать жалобы по команде <code>/report</code> (ответом на сообщение);\n"
        f"• публиковать правила под каждым постом канала.\n\n"
        f"Загляни в канал и присоединяйся к разговору 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Перейти в канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="💬 Чат обсуждения", url=CHANNEL_URL + "?comments=1")],
        ]
    )

    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# ──────────────────────────────────────────────────────────────
#  2. ГРУППА ОБСУЖДЕНИЯ: новый участник
# ──────────────────────────────────────────────────────────────

# Антидубль: Telegram может прислать и chat_member, и new_chat_members
# на один и тот же вход. Запоминаем (chat_id, user_id) на 60 секунд.
_recently_greeted: set[tuple[int, int]] = set()


async def _forget(key: tuple[int, int], delay: int = 60) -> None:
    await asyncio.sleep(delay)
    _recently_greeted.discard(key)


async def greet_new_member(bot: Bot, chat_id: int, user: User) -> None:
    """Отправляет приветствие и ставит его в очередь на удаление."""
    if user.is_bot:
        return

    key = (chat_id, user.id)
    if key in _recently_greeted:
        log.debug("Пропуск дубля приветствия для %s", user.id)
        return
    _recently_greeted.add(key)
    asyncio.create_task(_forget(key))

    template = random.choice(GREETING_TEMPLATES)
    text = (
        f"{template.format(m=mention(user))}\n\n"
        f"{name_card(user)}\n\n"
        f"<i>Расскажи пару слов о себе — и приятного общения!</i>\n"
        f"<i>Это сообщение исчезнет через {GREETING_TTL} сек.</i>"
    )

    try:
        sent = await bot.send_message(chat_id, text, disable_web_page_preview=True)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("Не удалось отправить приветствие: %s", e)
        return

    log.info("Поприветствовали %s (id=%s) в чате %s", full_name(user), user.id, chat_id)
    with suppress(Exception):
        stats.record_event("greeting", user_id=user.id, name=full_name(user))
    schedule_delete(bot, chat_id, sent.message_id)


# Вариант А: событие chat_member — самый надёжный способ
# (требует allowed_updates=["chat_member", ...] — включено в main()).
@router.chat_member(
    F.chat.id == DISCUSSION_CHAT_ID,
    ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER),
)
async def on_user_join(event: ChatMemberUpdated, bot: Bot) -> None:
    await greet_new_member(bot, event.chat.id, event.new_chat_member.user)


# Вариант Б: служебное сообщение new_chat_members — страховка
# на случай, если chat_member-апдейты недоступны.
@router.message(F.new_chat_members, F.chat.id == DISCUSSION_CHAT_ID)
async def on_new_chat_members(message: Message, bot: Bot) -> None:
    if DELETE_SERVICE_MESSAGE:
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await message.delete()

    for user in message.new_chat_members:
        await greet_new_member(bot, message.chat.id, user)


# Чистим служебное «X покинул группу», чтобы лента оставалась опрятной.
@router.message(F.left_chat_member, F.chat.id == DISCUSSION_CHAT_ID)
async def on_left_member(message: Message) -> None:
    if DELETE_SERVICE_MESSAGE:
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await message.delete()


# ──────────────────────────────────────────────────────────────
#  3. ПРАВИЛА ПОД КАЖДЫМ ПОСТОМ КАНАЛА
# ──────────────────────────────────────────────────────────────

# Для альбома Telegram присылает несколько сообщений с одним media_group_id.
# Ключ запоминается на сутки, чтобы правила появились только один раз.
_processed_channel_posts: set[str] = set()


async def _forget_channel_post(key: str, delay: int = 86400) -> None:
    await asyncio.sleep(delay)
    _processed_channel_posts.discard(key)


@router.message(
    F.chat.id == DISCUSSION_CHAT_ID,
    F.is_automatic_forward == True,
    F.sender_chat.id == CHANNEL_ID,
)
async def send_rules_under_channel_post(message: Message) -> None:
    """Отвечает на автопересланный пост, создавая первый комментарий с правилами."""
    if not RULES_ENABLED:
        return

    post_key = message.media_group_id or f"message:{message.message_id}"
    if post_key in _processed_channel_posts:
        return

    _processed_channel_posts.add(post_key)
    asyncio.create_task(_forget_channel_post(post_key))

    try:
        sent = await message.reply_photo(
            photo=rules_photo(),
            caption=RULES_TEXT,
        )
    except TelegramAPIError as error:
        _processed_channel_posts.discard(post_key)
        log.warning(
            "Не удалось отправить правила под постом %s: %s",
            message.message_id,
            error,
        )
        return

    log.info(
        "Правила отправлены первым комментарием: пост=%s, комментарий=%s",
        message.message_id,
        sent.message_id,
    )
    with suppress(Exception):
        stats.record_event("rules", message_id=message.message_id, thread_id=sent.message_id)


# ──────────────────────────────────────────────────────────────
#  СБОР СТАТИСТИКИ (middleware — не мешает обработчикам)
# ──────────────────────────────────────────────────────────────

def _post_kind(message: Message) -> str:
    if message.media_group_id:
        return "album"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.animation:
        return "gif"
    if message.document:
        return "file"
    if message.audio or message.voice:
        return "audio"
    if message.poll:
        return "poll"
    return "text"


def _origin_post_id(message: Message) -> int | None:
    """ID исходного поста канала для автопересланной копии."""
    if message.forward_from_message_id:
        return message.forward_from_message_id
    origin = message.forward_origin
    return getattr(origin, "message_id", None)


class StatsMiddleware(BaseMiddleware):
    """Пишет статистику по каждому апдейту, затем пропускает его дальше."""

    async def __call__(self, handler, event: Update, data: dict):
        with suppress(Exception):
            await asyncio.to_thread(self._track, event)
        return await handler(event, data)

    @staticmethod
    def _track(event: Update) -> None:
        post = event.channel_post
        if post is not None and post.chat.id == CHANNEL_ID:
            text = post.text or post.caption or ""
            stats.record_post(
                post.message_id,
                ts=int(post.date.timestamp()),
                kind=_post_kind(post),
                text_len=len(text),
                preview=text.replace("\n", " ").strip(),
            )
            return

        msg = event.message
        if msg is not None:
            if msg.chat.type == ChatType.PRIVATE:
                if msg.text and msg.text.startswith("/start") and msg.from_user:
                    stats.record_event(
                        "start", user_id=msg.from_user.id, name=full_name(msg.from_user)
                    )
                return

            if msg.chat.id != DISCUSSION_CHAT_ID:
                return

            if msg.is_automatic_forward:
                stats.link_thread(_origin_post_id(msg), msg.message_id)
                return

            if msg.new_chat_members:
                for user in msg.new_chat_members:
                    if not user.is_bot:
                        stats.upsert_member(
                            user.id, user.first_name or "", user.last_name or "",
                            user.username, joined=True,
                        )
                        stats.record_join(user.id, full_name(user), user.username)
                return

            if msg.left_chat_member and not msg.left_chat_member.is_bot:
                stats.record_leave(msg.left_chat_member.id, full_name(msg.left_chat_member))
                return

            user = msg.from_user
            if user and not user.is_bot:
                stats.upsert_member(
                    user.id, user.first_name or "", user.last_name or "", user.username
                )
                stats.record_comment(
                    user.id,
                    full_name(user),
                    thread_id=msg.message_thread_id,
                    message_id=msg.message_id,
                    text_len=len(msg.text or msg.caption or ""),
                )
            return

        member = event.chat_member
        if member is not None and member.chat.id == DISCUSSION_CHAT_ID:
            user = member.new_chat_member.user
            if user.is_bot:
                return
            was = member.old_chat_member.status
            now = member.new_chat_member.status
            joined = was in ("left", "kicked") and now in ("member", "administrator", "creator")
            left = was in ("member", "administrator", "creator") and now in ("left", "kicked")
            if joined:
                stats.upsert_member(
                    user.id, user.first_name or "", user.last_name or "",
                    user.username, joined=True,
                )
                stats.record_join(user.id, full_name(user), user.username)
            elif left:
                stats.record_leave(user.id, full_name(user))
            return

        reactions = event.message_reaction_count
        if reactions is not None and reactions.chat.id == CHANNEL_ID:
            total = sum(r.total_count for r in reactions.reactions)
            stats.record_reactions(reactions.message_id, total)


async def snapshot_loop(bot: Bot, interval: int = 600) -> None:
    """Периодически фиксирует число подписчиков канала и участников чата."""
    while True:
        subscribers = chat_members = None
        with suppress(Exception):
            subscribers = await bot.get_chat_member_count(CHANNEL_ID)
        with suppress(Exception):
            chat_members = await bot.get_chat_member_count(DISCUSSION_CHAT_ID)
        if subscribers is not None or chat_members is not None:
            with suppress(Exception):
                await asyncio.to_thread(stats.save_snapshot, subscribers, chat_members)
        await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────
#  ЗАПУСК
# ──────────────────────────────────────────────────────────────

async def on_startup(bot: Bot) -> None:
    me = await bot.get_me()
    log.info("Бот запущен: @%s (id=%s)", me.username, me.id)

    with suppress(Exception):
        chat = await bot.get_chat(DISCUSSION_CHAT_ID)
        member = await bot.get_chat_member(DISCUSSION_CHAT_ID, me.id)
        log.info("Чат обсуждения: «%s» | статус бота: %s", chat.title, member.status)
        if member.status != ChatMemberStatus.ADMINISTRATOR:
            log.warning(
                "Бот НЕ администратор в чате обсуждения — "
                "удаление сообщений работать не будет!"
            )


async def main() -> None:
    if BOT_TOKEN.startswith("PASTE"):
        raise SystemExit("❌ Укажите BOT_TOKEN в переменных окружения или в .env")

    ensure_rules_image()
    stats.init()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.update.outer_middleware(StatsMiddleware())
    dp.include_router(router)            # приветствия, правила, /start
    dp.include_router(moderation.router) # модерация и антиспам
    dp.startup.register(on_startup)

    # Веб-интерфейс на порту 3000 → https://serp.bothost.tech
    runner = await start_web(bot)
    snapshots = asyncio.create_task(snapshot_loop(bot))

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "edited_message",
                "channel_post",
                "edited_channel_post",
                "chat_member",
                "my_chat_member",
                "message_reaction",
                "message_reaction_count",
            ],
        )
    finally:
        snapshots.cancel()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
