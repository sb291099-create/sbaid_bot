"""Планировщик: сроки заявок и сделок, напоминания, резервы, сводка, возвраты."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .core import exits, market, orders, placements, trades
from .core.context import App

log = logging.getLogger(__name__)


async def tick(app: App) -> None:
    steps = {
        "резервы": placements.expire_reservations,
        "окончание размещений": placements.close_expired,
        "сроки заявок": orders.expire,
        "сроки сделок": trades.process_deadlines,
        "напоминания": trades.send_reminders,
        "сводка в канал": market.daily_summary,
    }
    for name, fn in steps.items():
        try:
            async with app.tx() as ctx:
                await fn(ctx)
        except Exception:  # noqa: BLE001
            log.exception("Сбой задачи «%s»", name)
    try:
        await exits.process_queue(app)
    except Exception:  # noqa: BLE001
        log.exception("Сбой очереди возвратов")


def build_scheduler(app: App) -> AsyncIOScheduler:
    sch = AsyncIOScheduler(timezone="Europe/Moscow")
    sch.add_job(tick, "interval", minutes=1, args=[app], max_instances=1, coalesce=True)
    return sch
