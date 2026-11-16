"""HTTP-сервер для уведомлений Robokassa (ResultURL) и проверки живости."""
from __future__ import annotations

import logging

from aiohttp import web

from .core import placements
from .core.context import App

log = logging.getLogger(__name__)


def build_web(app: App) -> web.Application:
    async def result(request: web.Request) -> web.Response:
        params = dict(request.query)
        if request.method == "POST":
            params.update({k: str(v) for k, v in (await request.post()).items()})
        note = app.payments.verify_result(params) if app.payments else None
        if note is None:
            log.warning("Robokassa: неверная подпись уведомления, InvId=%s", params.get("InvId"))
            return web.Response(status=400, text="bad sign")
        async with app.tx() as ctx:
            status = await placements.on_payment(ctx, note.inv_id, note.amount)
        log.info("Robokassa: счёт %s — %s", note.inv_id, status)
        if status in ("unknown", "mismatch"):
            return web.Response(status=400, text="bad invoice")
        # Robokassa ждёт ровно такой ответ, иначе будет повторять уведомление
        return web.Response(text=f"OK{note.inv_id}")

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    w = web.Application()
    w.router.add_route("*", "/robokassa/result", result)
    w.router.add_get("/health", health)
    return w
