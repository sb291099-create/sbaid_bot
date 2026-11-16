"""Запуск: бот (long polling), HTTP-сервер для Robokassa и планировщик."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiohttp import web

from .config import Config
from .core.context import App
from .crypto import Cipher
from .db import init_db, make_engine, make_sessionmaker
from .payments.robokassa import Robokassa
from .scheduler import build_scheduler
from .telegram.bot import build_dispatcher
from .telegram.notifier import TelegramNotifier
from .web import build_web


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    if not cfg.bot_token:
        raise SystemExit("Не задан BOT_TOKEN")
    engine = make_engine(cfg.database_url)
    await init_db(engine, cfg.total_shares)
    sm = make_sessionmaker(engine)
    bot = Bot(cfg.bot_token)
    rk = Robokassa(cfg.robokassa_login, cfg.robokassa_password1, cfg.robokassa_password2,
                   cfg.robokassa_password3, cfg.robokassa_test_mode, cfg.robokassa_sno)
    app = App(sm, cfg, TelegramNotifier(bot, sm, cfg), Cipher(cfg.encryption_key), rk)

    runner = web.AppRunner(build_web(app))
    await runner.setup()
    await web.TCPSite(runner, cfg.web_host, cfg.web_port).start()
    scheduler = build_scheduler(app)
    scheduler.start()
    try:
        await build_dispatcher(app).start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
