"""
HTTP-сервер бота: веб-интерфейс со статистикой канала.

Слушает 0.0.0.0:3000 — этот порт хостинг пробрасывает
на https://serp.bothost.tech с автоматическим SSL.

Роуты:
  GET /             — дашборд (index.html из корня проекта)
  GET /health       — health-check
  GET /api/status   — краткий статус сервиса
  GET /api/stats    — полная статистика (?days=7|30|90)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from aiohttp import web

import stats

log = logging.getLogger("web")

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "3000"))
DOMAIN = os.getenv("DOMAIN", "serp.bothost.tech")

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004339622999"))
DISCUSSION_CHAT_ID = int(os.getenv("DISCUSSION_CHAT_ID", "-1004332798085"))
CHANNEL_SHORT_ID = str(CHANNEL_ID).replace("-100", "", 1)

BASE_DIR = Path(__file__).parent
INDEX_FILE = BASE_DIR / "index.html"
RULES_IMAGE = BASE_DIR / Path(os.getenv("RULES_IMAGE", "rules.png")).name

STARTED_AT = time.time()

# Кэш «живых» чисел из Telegram, чтобы не дёргать API на каждый запрос.
_live_cache: dict[str, object] = {"ts": 0.0, "data": {}}
_LIVE_TTL = 45.0


# ──────────────────────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНОЕ
# ──────────────────────────────────────────────────────────────

async def _live_numbers(bot: object | None) -> dict[str, object]:
    """Подписчики канала, участники чата и имя бота (с кэшем)."""
    now = time.time()
    if bot is None:
        return {"subscribers": None, "chat_members": None, "bot": None, "title": None}

    if now - float(_live_cache["ts"]) < _LIVE_TTL and _live_cache["data"]:
        return dict(_live_cache["data"])  # type: ignore[arg-type]

    data: dict[str, object] = {
        "subscribers": None,
        "chat_members": None,
        "bot": None,
        "title": None,
    }
    try:
        me = await bot.get_me()  # type: ignore[attr-defined]
        data["bot"] = f"@{me.username}"
    except Exception:  # noqa: BLE001
        pass
    try:
        data["subscribers"] = await bot.get_chat_member_count(CHANNEL_ID)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        data["chat_members"] = await bot.get_chat_member_count(DISCUSSION_CHAT_ID)  # type: ignore[attr-defined]
        chat = await bot.get_chat(CHANNEL_ID)  # type: ignore[attr-defined]
        data["title"] = chat.title
    except Exception:  # noqa: BLE001
        pass

    _live_cache["ts"] = now
    _live_cache["data"] = data
    return dict(data)


# ──────────────────────────────────────────────────────────────
#  РОУТЫ
# ──────────────────────────────────────────────────────────────

async def index(request: web.Request) -> web.StreamResponse:
    if INDEX_FILE.exists():
        return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})
    return web.Response(text="Bot web interface is running.", content_type="text/plain")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "uptime": round(time.time() - STARTED_AT, 1)})


async def api_status(request: web.Request) -> web.Response:
    live = await _live_numbers(request.app.get("bot"))
    return web.json_response(
        {
            "service": "channel-bot",
            "bot": live.get("bot") or "unknown",
            "domain": DOMAIN,
            "port": WEB_PORT,
            "uptime_sec": round(time.time() - STARTED_AT, 1),
            "rules_image": RULES_IMAGE.name if RULES_IMAGE.exists() else None,
            "endpoints": ["/", "/health", "/api/status", "/api/stats"],
        }
    )


async def api_stats(request: web.Request) -> web.Response:
    try:
        days = max(1, min(365, int(request.query.get("days", "30"))))
    except ValueError:
        days = 30

    payload, live = await asyncio.gather(
        stats.build_stats_async(days, CHANNEL_SHORT_ID),
        _live_numbers(request.app.get("bot")),
    )
    payload["live"] = {
        **live,
        "uptime_sec": round(time.time() - STARTED_AT, 1),
        "domain": DOMAIN,
        "rules_image": RULES_IMAGE.exists(),
        "channel_id": CHANNEL_ID,
        "chat_id": DISCUSSION_CHAT_ID,
    }
    return web.json_response(payload, headers={"Cache-Control": "no-store"})


async def handle_404(request: web.Request) -> web.Response:
    return web.json_response({"error": "not found", "path": request.path}, status=404)


# ──────────────────────────────────────────────────────────────
#  ПРИЛОЖЕНИЕ
# ──────────────────────────────────────────────────────────────

def create_app(bot: object | None = None) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_route("*", "/{tail:.*}", handle_404)
    return app


async def start_web(bot: object | None = None) -> web.AppRunner:
    """Поднимает HTTP-сервер в фоне и возвращает runner для корректной остановки."""
    stats.init()
    runner = web.AppRunner(create_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    log.info("HTTP-сервер слушает %s:%s → https://%s", WEB_HOST, WEB_PORT, DOMAIN)
    return runner


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    stats.init()
    web.run_app(create_app(), host=WEB_HOST, port=WEB_PORT)
