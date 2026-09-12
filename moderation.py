"""
Антиспам-фильтр и команды модерации.

Фильтр рассчитан на обфусцированные рассылки вида
«Бееслпаттnoe Dettck00ee … Сmotpuu в поuиске: KZC59»:
текст нормализуется (снятие диакритики, гомоглифы, цифры вместо букв,
схлопывание повторов), затем оценивается по набору признаков.

Команды (только админы, включая анонимных):
  !мут  [срок] [причина]
  !варн [причина]
  !бан  [срок] [причина]
  !размут / !разбан / !снятьварн
Команды работают по reply и по @username.

Для всех участников:
  /report — жалоба на сообщение (по reply).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from contextlib import suppress
from difflib import SequenceMatcher

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, Message

import stats

log = logging.getLogger("moderation")

DISCUSSION_CHAT_ID = int(os.getenv("DISCUSSION_CHAT_ID", "-1004332798085"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004339622999"))

WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))            # варнов до автомута
WARN_MUTE_SECONDS = int(os.getenv("WARN_MUTE", "86400"))  # длительность автомута
SPAM_THRESHOLD = int(os.getenv("SPAM_THRESHOLD", "4"))    # порог срабатывания фильтра
NOTICE_TTL = int(os.getenv("NOTICE_TTL", "20"))           # сек, жизнь служебных ответов

router = Router(name="moderation")


# ──────────────────────────────────────────────────────────────
#  НОРМАЛИЗАЦИЯ ТЕКСТА
# ──────────────────────────────────────────────────────────────

CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "j",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "c", "ш": "s", "щ": "s", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "u",
    "я": "a", "і": "i", "ї": "i", "є": "e", "ґ": "g", "ў": "u",
}
DIGIT2LAT = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "6": "b", "7": "t", "9": "g"}

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF]"
)
INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060-\u206f\ufe00-\ufe0f\u180e]")
# Код-приманка вида KZC59 / THK36 / YVX 37
CODE_RE = re.compile(r"\b([A-Za-zА-Яа-яЁё]{2,5})\s?[-–—]?\s?(\d{2,4})\b")
CYR_RE = re.compile(r"[а-яёА-ЯЁ]")
LAT_RE = re.compile(r"[a-zA-Z]")


def skeleton(text: str) -> str:
    """Приводит обфусцированный текст к «скелету»: латиница, без повторов и мусора."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = unicodedata.normalize("NFKC", text).lower()

    out = []
    for ch in text:
        if ch in CYR2LAT:
            out.append(CYR2LAT[ch])
        elif ch in DIGIT2LAT:
            out.append(DIGIT2LAT[ch])
        elif "a" <= ch <= "z":
            out.append(ch)
        else:
            out.append(" ")
    result = re.sub(r"(.)\1+", r"\1", "".join(out))
    return re.sub(r"\s+", " ", result).strip()


def _kw(word: str) -> str:
    return skeleton(word)


# Ключевые слова в «скелетном» виде (детская тематика, приманки, зазывы).
KEYWORDS_STRONG = [
    _kw(w) for w in (
        "детское", "дество", "школьное", "школьницы", "малолетки", "подростки",
        "порно", "порево", "инцест", "слив", "слитое", "cp", "педо",
    )
]
KEYWORDS_BAIT = [
    _kw(w) for w in (
        "бесплатно", "бесплатное", "в поиске", "поиске", "поиск", "вбивай",
        "вбейка", "вбей", "смотри", "то самое", "архив", "тгк",
    )
]


def _fuzzy_hit(tokens: list[str], keywords: list[str], ratio: float = 0.78) -> list[str]:
    """Нечёткое совпадение токенов с ключевыми словами (устойчиво к опечаткам)."""
    hits: list[str] = []
    for kw in keywords:
        if not kw or len(kw) < 3:
            continue
        if kw in tokens:
            hits.append(kw)
            continue
        for tok in tokens:
            if abs(len(tok) - len(kw)) > 4 or len(tok) < 3:
                continue
            if SequenceMatcher(None, tok, kw).ratio() >= ratio:
                hits.append(kw)
                break
    return hits


def analyze(text: str) -> tuple[int, list[str]]:
    """Возвращает (оценку спама, список сработавших признаков)."""
    if not text or len(text.strip()) < 6:
        return 0, []

    raw = text[:2000]
    score = 0
    reasons: list[str] = []

    skel = skeleton(raw)
    tokens = skel.split()
    glued = skel.replace(" ", "")

    # 1. Код-приманка, повторяющийся в сообщении.
    codes = [f"{m.group(1).lower()}{m.group(2)}" for m in CODE_RE.finditer(raw)]
    if codes:
        top = max(set(codes), key=codes.count)
        repeats = codes.count(top)
        if repeats >= 2:
            score += 3
            reasons.append(f"код-приманка «{top.upper()}» ×{repeats}")
        else:
            score += 1
            reasons.append("код-приманка")

    # 2. Ключевые слова.
    strong = _fuzzy_hit(tokens, KEYWORDS_STRONG)
    if not strong:
        strong = [k for k in KEYWORDS_STRONG if len(k) >= 5 and k in glued]
    if strong:
        score += 3 if len(strong) > 1 else 2
        reasons.append("запрещённая тематика: " + ", ".join(sorted(set(strong))[:3]))

    bait = _fuzzy_hit(tokens, KEYWORDS_BAIT)
    if bait:
        score += 2 if len(bait) > 1 else 1
        reasons.append("зазывные фразы: " + ", ".join(sorted(set(bait))[:3]))

    # 3. Диакритика-обфускация (Ḅ P̣ỌỊṢḲẸẸ).
    combining = sum(1 for ch in unicodedata.normalize("NFKD", raw) if unicodedata.combining(ch))
    if combining >= 3:
        score += 2
        reasons.append("маскировка диакритикой")

    # 4. Смешение кириллицы и латиницы внутри слов.
    mixed = sum(
        1 for word in re.findall(r"\S+", raw)
        if len(word) > 3 and CYR_RE.search(word) and LAT_RE.search(word)
    )
    if mixed >= 2:
        score += 2
        reasons.append(f"гомоглифы в {mixed} словах")
    elif mixed == 1:
        score += 1

    # 5. Невидимые символы.
    if len(INVISIBLE_RE.findall(raw)) >= 2:
        score += 1
        reasons.append("невидимые символы")

    # 6. Эмодзи-шум.
    emojis = len(EMOJI_RE.findall(raw))
    if emojis >= 8:
        score += 2
        reasons.append(f"эмодзи-шум ×{emojis}")
    elif emojis >= 4:
        score += 1

    # 7. Повторяющиеся строки.
    lines = [ln.strip() for ln in raw.splitlines() if len(ln.strip()) > 4]
    if lines and len(lines) - len(set(lines)) >= 1:
        score += 1
        reasons.append("дублирование строк")

    # 8. Растянутые буквы («вбивбиевай», «ееее»).
    if len(re.findall(r"(.)\1{2,}", raw)) >= 2:
        score += 1
        reasons.append("растянутые буквы")

    return score, reasons


# ──────────────────────────────────────────────────────────────
#  УТИЛИТЫ
# ──────────────────────────────────────────────────────────────

MUTE_OFF = ChatPermissions(
    can_send_messages=False, can_send_audios=False, can_send_documents=False,
    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
    can_add_web_page_previews=False, can_change_info=False, can_invite_users=False,
    can_pin_messages=False, can_manage_topics=False,
)
MUTE_ON = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True,
)

UNITS = {
    "s": 1, "sec": 1, "с": 1, "сек": 1,
    "m": 60, "min": 60, "м": 60, "мин": 60,
    "h": 3600, "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "d": 86400, "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400,
    "w": 604800, "н": 604800, "нед": 604800, "неделя": 604800,
    "mo": 2592000, "мес": 2592000, "месяц": 2592000,
}
FOREVER = {"навсегда", "перм", "forever", "perm", "∞"}
DUR_RE = re.compile(r"^(\d+)\s*([a-zA-Zа-яА-ЯёЁ]*)$")


def parse_duration(token: str) -> tuple[int | None, bool]:
    """('30м') → (1800, True). Возвращает (секунды|None, распознано ли)."""
    if not token:
        return None, False
    low = token.lower().strip()
    if low in FOREVER:
        return None, True
    m = DUR_RE.match(low)
    if not m:
        return None, False
    value = int(m.group(1))
    unit = m.group(2) or "m"
    if unit not in UNITS:
        return None, False
    return max(30, value * UNITS[unit]), True


def human_duration(seconds: int | None) -> str:
    if not seconds:
        return "навсегда"
    for limit, unit, name in ((86400, 86400, "дн"), (3600, 3600, "ч"), (60, 60, "мин")):
        if seconds >= limit:
            return f"{seconds // unit} {name}"
    return f"{seconds} сек"


def esc(text: str) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def user_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Участник"


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'


async def is_admin(message: Message, bot: Bot) -> bool:
    """Проверка прав, включая анонимных админов и отправку от имени канала."""
    if message.sender_chat:
        return message.sender_chat.id in (message.chat.id, CHANNEL_ID)
    if not message.from_user:
        return False
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except TelegramAPIError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


def moderator_name(message: Message) -> str:
    if message.sender_chat:
        return message.author_signature or message.sender_chat.title or "Админ"
    return user_name(message.from_user) if message.from_user else "Админ"


async def notice(message: Message, text: str, ttl: int = NOTICE_TTL) -> None:
    """Служебный ответ, который сам исчезает."""
    with suppress(TelegramAPIError):
        sent = await message.answer(text, disable_web_page_preview=True)
        if ttl:
            asyncio.create_task(_delete_later(message.bot, sent.chat.id, sent.message_id, ttl))


async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    with suppress(TelegramAPIError):
        await bot.delete_message(chat_id, message_id)


async def resolve_target(message: Message, args: list[str]) -> tuple[int | None, str, list[str]]:
    """Определяет цель команды: по reply, @username или числовому id."""
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        return reply.from_user.id, user_name(reply.from_user), args

    if args:
        raw = args[0].strip()
        token = raw.replace("https://t.me/", "").replace("t.me/", "").lstrip("@")
        if token.isdigit():
            return int(token), f"ID {token}", args[1:]
        if raw.startswith("@") or "t.me/" in raw:
            found = await asyncio.to_thread(stats.find_user_by_username, token)
            if found:
                return found["user_id"], found["name"], args[1:]
            return None, token, args[1:]
    return None, "", args


def parse_command(text: str) -> list[str]:
    return (text or "").split()[1:]


# ──────────────────────────────────────────────────────────────
#  КОМАНДЫ МОДЕРАЦИИ
# ──────────────────────────────────────────────────────────────

IN_CHAT = F.chat.id == DISCUSSION_CHAT_ID


async def _guard(message: Message, bot: Bot) -> bool:
    if await is_admin(message, bot):
        return True
    await notice(message, "🚫 Команда доступна только администраторам.", 8)
    with suppress(TelegramAPIError):
        await message.delete()
    return False


async def _no_target(message: Message) -> None:
    await notice(
        message,
        "⚠️ Не понял, к кому применить. Ответьте на сообщение участника "
        "или укажите <code>@username</code>.\n"
        "<i>Поиск по @username работает для тех, кто уже писал в чате.</i>",
        12,
    )


@router.message(IN_CHAT, F.text.regexp(r"(?i)^\s*[!/]\s*(варн|warn|пред)\b"))
async def cmd_warn(message: Message, bot: Bot) -> None:
    if not await _guard(message, bot):
        return

    args = parse_command(message.text)
    user_id, name, rest = await resolve_target(message, args)
    if not user_id:
        await _no_target(message)
        return

    reason = " ".join(rest).strip() or "без указания причины"
    moderator = moderator_name(message)
    count = await asyncio.to_thread(stats.add_warn, user_id, name, moderator, reason)

    text = (
        f"⚠️ <b>Предупреждение {count}/{WARN_LIMIT}</b>\n"
        f"Участник: {mention(user_id, name)}\n"
        f"Модератор: {esc(moderator)}\n"
        f"Причина: {esc(reason)}"
    )

    if count >= WARN_LIMIT:
        ok = await apply_mute(bot, message.chat.id, user_id, WARN_MUTE_SECONDS)
        if ok:
            await asyncio.to_thread(stats.reset_warns, user_id)
            await asyncio.to_thread(
                stats.add_punishment, user_id, name, "mute",
                int(time.time()) + WARN_MUTE_SECONDS, moderator, "лимит предупреждений",
            )
            text += (
                f"\n\n🔇 Лимит исчерпан — мут на "
                f"{human_duration(WARN_MUTE_SECONDS)}. Счётчик обнулён."
            )

    with suppress(TelegramAPIError):
        await message.answer(text, disable_web_page_preview=True)
    with suppress(TelegramAPIError):
        await message.delete()


async def apply_mute(bot: Bot, chat_id: int, user_id: int, seconds: int | None) -> bool:
    until = int(time.time()) + seconds if seconds else None
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=MUTE_OFF, until_date=until)
        return True
    except TelegramAPIError as e:
        log.warning("Мут не применён (%s): %s", user_id, e)
        return False


@router.message(IN_CHAT, F.text.regexp(r"(?i)^\s*[!/]\s*(мут|mute|молчание)\b"))
async def cmd_mute(message: Message, bot: Bot) -> None:
    if not await _guard(message, bot):
        return

    args = parse_command(message.text)
    user_id, name, rest = await resolve_target(message, args)
    if not user_id:
        await _no_target(message)
        return

    seconds: int | None = 3600
    if rest:
        parsed, ok = parse_duration(rest[0])
        if ok:
            seconds, rest = parsed, rest[1:]

    reason = " ".join(rest).strip() or "без указания причины"
    moderator = moderator_name(message)

    if not await apply_mute(bot, message.chat.id, user_id, seconds):
        await notice(message, "❌ Не удалось выдать мут — проверьте права бота.", 10)
        return

    await asyncio.to_thread(
        stats.add_punishment, user_id, name, "mute",
        int(time.time()) + seconds if seconds else None, moderator, reason,
    )

    with suppress(TelegramAPIError):
        await message.answer(
            f"🔇 <b>Мут выдан</b>\n"
            f"Участник: {mention(user_id, name)}\n"
            f"Срок: {human_duration(seconds)}\n"
            f"Модератор: {esc(moderator)}\n"
            f"Причина: {esc(reason)}",
            disable_web_page_preview=True,
        )
    with suppress(TelegramAPIError):
        await message.delete()


@router.message(IN_CHAT, F.text.regexp(r"(?i)^\s*[!/]\s*(бан|ban|забанить)\b"))
async def cmd_ban(message: Message, bot: Bot) -> None:
    if not await _guard(message, bot):
        return

    args = parse_command(message.text)
    user_id, name, rest = await resolve_target(message, args)
    if not user_id:
        await _no_target(message)
        return

    seconds: int | None = None  # по умолчанию — навсегда
    if rest:
        parsed, ok = parse_duration(rest[0])
        if ok:
            seconds, rest = parsed, rest[1:]

    reason = " ".join(rest).strip() or "без указания причины"
    moderator = moderator_name(message)
    until = int(time.time()) + seconds if seconds else None

    try:
        await bot.ban_chat_member(message.chat.id, user_id, until_date=until)
    except TelegramAPIError as e:
        log.warning("Бан не применён (%s): %s", user_id, e)
        await notice(message, "❌ Не удалось забанить — проверьте права бота.", 10)
        return

    await asyncio.to_thread(
        stats.add_punishment, user_id, name, "ban", until, moderator, reason
    )

    with suppress(TelegramAPIError):
        await message.answer(
            f"🔨 <b>Бан выдан</b>\n"
            f"Участник: {mention(user_id, name)}\n"
            f"Срок: {human_duration(seconds)}\n"
            f"Модератор: {esc(moderator)}\n"
            f"Причина: {esc(reason)}",
            disable_web_page_preview=True,
        )
    with suppress(TelegramAPIError):
        await message.delete()


@router.message(IN_CHAT, F.text.regexp(r"(?i)^\s*[!/]\s*(размут|анмут|unmute)\b"))
async def cmd_unmute(message: Message, bot: Bot) -> None:
    if not await _guard(message, bot):
        return
    user_id, name, _ = await resolve_target(message, parse_command(message.text))
    if not user_id:
        await _no_target(message)
        return
    with suppress(TelegramAPIError):
        await bot.restrict_chat_member(message.chat.id, user_id, permissions=MUTE_ON)
    await asyncio.to_thread(
        stats.add_punishment, user_id, name, "unmute", None, moderator_name(message), "снятие мута"
    )
    await notice(message, f"🔊 Мут снят: {mention(user_id, name)}", 15)
    with suppress(TelegramAPIError):
        await message.delete()


@router.message(IN_CHAT, F.text.regexp(r"(?i)^\s*[!/]\s*(разбан|унбан|unban)\b"))
async def cmd_unban(message: Message, bot: Bot) -> None:
    if not await _guard(message, bot):
        return
    user_id, name, _ = await resolve_target(message, parse_command(message.text))
    if not user_id:
        await _no_target(message)
        return
    with suppress(TelegramAPIError):
        await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
    await asyncio.to_thread(
        stats.add_punishment, user_id, name, "unban", None, moderator_name(message), "снятие бана"
    )
    await notice(message, f"✅ Бан снят: {mention(user_id, name)}", 15)
    with suppress(TelegramAPIError):
        await message.delete()


@router.message(IN_CHAT, F.text.regexp(r"(?i)^\s*[!/]\s*(снятьварн|unwarn|снятьпред)\b"))
async def cmd_unwarn(message: Message, bot: Bot) -> None:
    if not await _guard(message, bot):
        return
    user_id, name, _ = await resolve_target(message, parse_command(message.text))
    if not user_id:
        await _no_target(message)
        return
    removed = await asyncio.to_thread(stats.reset_warns, user_id)
    await notice(message, f"🧹 Снято предупреждений: {removed} · {mention(user_id, name)}", 15)
    with suppress(TelegramAPIError):
        await message.delete()


# ──────────────────────────────────────────────────────────────
#  /report — для всех участников
# ──────────────────────────────────────────────────────────────

@router.message(IN_CHAT, F.text.regexp(r"(?i)^\s*[!/]\s*(report|репорт|жалоба)\b"))
async def cmd_report(message: Message, bot: Bot) -> None:
    reply = message.reply_to_message
    if not reply:
        await notice(
            message,
            "ℹ️ Ответьте командой <code>/report</code> на сообщение, "
            "которое нужно передать администрации.",
            12,
        )
        with suppress(TelegramAPIError):
            await message.delete()
        return

    reporter = message.from_user
    reporter_name = user_name(reporter) if reporter else "Аноним"
    target = reply.from_user
    target_id = target.id if target and not target.is_bot else None
    target_name = user_name(target) if target else (
        reply.sender_chat.title if reply.sender_chat else "автор сообщения"
    )
    comment = " ".join(parse_command(message.text)).strip()
    content = (reply.text or reply.caption or "[медиа без текста]")[:300]

    report_id = await asyncio.to_thread(
        stats.add_report, reporter.id if reporter else 0, reporter_name,
        target_id, target_name, reply.message_id, content,
    )

    link = reply.get_url() or ""
    card = (
        f"🚨 <b>Жалоба #{report_id}</b>\n\n"
        f"От: {mention(reporter.id, reporter_name) if reporter else 'аноним'}\n"
        f"На: {mention(target_id, target_name) if target_id else esc(target_name)}\n"
        + (f"Комментарий: {esc(comment)}\n" if comment else "")
        + f"\n<b>Сообщение:</b>\n<blockquote>{esc(content)}</blockquote>"
        + (f'\n\n<a href="{link}">Перейти к сообщению</a>' if link else "")
    )

    delivered = 0
    with suppress(TelegramAPIError):
        for admin in await bot.get_chat_administrators(message.chat.id):
            if admin.user.is_bot:
                continue
            try:
                await bot.send_message(admin.user.id, card, disable_web_page_preview=True)
                delivered += 1
            except TelegramAPIError:
                continue

    await notice(
        message,
        f"✅ Жалоба #{report_id} принята. Администрация уведомлена"
        + (f" ({delivered})." if delivered else "."),
        12,
    )
    with suppress(TelegramAPIError):
        await message.delete()


# ──────────────────────────────────────────────────────────────
#  АНТИСПАМ-ФИЛЬТР (последним — чтобы не перехватывать команды)
# ──────────────────────────────────────────────────────────────

@router.message(IN_CHAT, F.content_type.in_({"text", "photo", "video", "animation", "document"}))
async def antispam(message: Message, bot: Bot) -> None:
    text = message.text or message.caption or ""
    if not text:
        return
    if message.is_automatic_forward or message.sender_chat:
        return

    score, reasons = analyze(text)
    if score < SPAM_THRESHOLD:
        return

    user = message.from_user
    if not user or user.is_bot:
        return
    if await is_admin(message, bot):
        return

    with suppress(TelegramAPIError):
        await message.delete()

    name = user_name(user)
    count = await asyncio.to_thread(
        stats.add_warn, user.id, name, "Антиспам", "; ".join(reasons[:3]) or "спам-рассылка"
    )
    await asyncio.to_thread(
        stats.record_event, "spam",
        user_id=user.id, name=name, value=score, meta={"reasons": reasons},
    )
    log.info("Спам удалён у %s (score=%s): %s", user.id, score, reasons)

    warning = (
        f"🛡 <b>Сообщение удалено автоматически</b>\n"
        f"Участник: {mention(user.id, name)}\n"
        f"Предупреждение: <b>{count}/{WARN_LIMIT}</b>\n"
        f"Признаки: {esc(', '.join(reasons[:3]))}"
    )

    if count >= WARN_LIMIT:
        if await apply_mute(bot, message.chat.id, user.id, WARN_MUTE_SECONDS):
            await asyncio.to_thread(stats.reset_warns, user.id)
            await asyncio.to_thread(
                stats.add_punishment, user.id, name, "mute",
                int(time.time()) + WARN_MUTE_SECONDS, "Антиспам", "спам-рассылка",
            )
            warning += f"\n\n🔇 Мут на {human_duration(WARN_MUTE_SECONDS)} — лимит исчерпан."

    with suppress(TelegramAPIError):
        sent = await message.answer(warning, disable_web_page_preview=True)
        asyncio.create_task(_delete_later(bot, sent.chat.id, sent.message_id, 30))
