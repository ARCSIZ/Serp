"""
Telegram-бот для канала и группы обсуждения.

Возможности:
  1. /start в личке — красивое приветствие пользователя.
  2. Приветствие новых участников в группе обсуждения канала
     (упоминание по имени/фамилии + авто-удаление через 10 секунд).

Стек: Python 3.10+, aiogram 3.x
Запуск: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import ChatMemberUpdatedFilter, CommandStart, IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("channel-bot")

router = Router(name="main")


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
        f"• поддерживать порядок и чистоту в ленте;\n"
        f"• подсказывать, где найти свежие публикации.\n\n"
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

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    # Веб-интерфейс на порту 3000 → https://serp.bothost.tech
    runner = await start_web(bot)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "edited_message", "chat_member", "my_chat_member"],
        )
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
