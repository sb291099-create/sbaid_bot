"""Планировщик: торговый день, сроки сделок, напоминания, резервы, возвраты."""
from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .core import exits, placements, trades, trading
from .core.context import App
from .core.errors import DomainError

log = logging.getLogger(__name__)

# Автозапуск торгового дня возможен в течение часа после отсечки. Если бот в это
# время лежал, день проводит админ командой /tradingday.
AUTO_RUN_WINDOW = timedelta(hours=1)


async def tick(app: App) -> None:
    steps = {
        "резервы": placements.expire_reservations,
        "окончание размещений": placements.close_expired,
        "сроки сделок": trades.process_deadlines,
        "напоминания": trades.send_reminders,
    }
    for name, fn in steps.items():
        try:
            async with app.tx() as ctx:
                await fn(ctx)
        except Exception:  # noqa: BLE001
            log.exception("Сбой задачи «%s»", name)
    try:
        await run_trading_day_if_due(app)
    except Exception:  # noqa: BLE001
        log.exception("Сбой торгового дня")
        await app.notifier.notify_staff("❗ Плановый торговый день не прошёл. Запустите вручную: /tradingday")
    try:
        await exits.process_queue(app)
    except Exception:  # noqa: BLE001
        log.exception("Сбой очереди возвратов")


async def run_trading_day_if_due(app: App) -> bool:
    async with app.tx() as ctx:
        cutoff = await trading.last_cutoff(ctx)
        if ctx.now - cutoff > AUTO_RUN_WINDOW:
            return False
        try:
            await trading.run(ctx, cutoff)
        except DomainError:  # уже проведён
            return False
    return True


def build_scheduler(app: App) -> AsyncIOScheduler:
    sch = AsyncIOScheduler(timezone="Europe/Moscow")
    sch.add_job(tick, "interval", minutes=1, args=[app], max_instances=1, coalesce=True)
    return sch
