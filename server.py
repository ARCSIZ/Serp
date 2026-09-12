"""
HTTP-сервер бота (админка / mini app / health-check).

Слушает 0.0.0.0:3000 — именно этот порт пробрасывает хостинг bothost.tech
на домен https://serp.bothost.tech с автоматическим SSL.

Пока это пустая заготовка: отдаёт корневой index.html и служебные JSON-роуты.
Позже сюда добавляются админка, API и вебхук Telegram.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from aiohttp import web

log = logging.getLogger("web")

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "3000"))
DOMAIN = os.getenv("DOMAIN", "serp.bothost.tech")

BASE_DIR = Path(__file__).parent
INDEX_FILE = BASE_DIR / "index.html"

STARTED_AT = time.time()


# ──────────────────────────────────────────────────────────────
#  РОУТЫ
# ──────────────────────────────────────────────────────────────

async def index(request: web.Request) -> web.StreamResponse:
    """Главная страница — пустая заготовка интерфейса."""
    if INDEX_FILE.exists():
        return web.FileResponse(INDEX_FILE)
    return web.Response(text="Bot web interface is running.", content_type="text/plain")


async def health(request: web.Request) -> web.Response:
    """Health-check для хостинга и мониторинга."""
    return web.json_response({"status": "ok", "uptime": round(time.time() - STARTED_AT, 1)})


async def api_status(request: web.Request) -> web.Response:
    """Базовая информация о сервисе."""
    bot: object | None = request.app.get("bot")
    username = None
    if bot is not None:
        try:
            me = await bot.get_me()  # type: ignore[attr-defined]
            username = me.username
        except Exception:  # noqa: BLE001
            username = None

    return web.json_response(
        {
            "service": "channel-bot",
            "bot": f"@{username}" if username else "unknown",
            "domain": DOMAIN,
            "port": WEB_PORT,
            "uptime_sec": round(time.time() - STARTED_AT, 1),
            "endpoints": ["/", "/health", "/api/status"],
        }
    )


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
    app.router.add_route("*", "/{tail:.*}", handle_404)
    return app


async def start_web(bot: object | None = None) -> web.AppRunner:
    """Поднимает HTTP-сервер в фоне и возвращает runner для корректной остановки."""
    app = create_app(bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    log.info("HTTP-сервер слушает %s:%s → https://%s", WEB_HOST, WEB_PORT, DOMAIN)
    return runner


if __name__ == "__main__":
    # Автономный запуск только веб-интерфейса: python server.py
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    web.run_app(create_app(), host=WEB_HOST, port=WEB_PORT)
