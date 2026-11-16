"""Заявки на рынке между участниками."""
from __future__ import annotations

from datetime import time, timedelta

from sqlalchemy import select

from ..models import Order, PriceHistory, User
from ..timeutil import next_weekday_at, rub
from . import registry, settings, users
from .audit import audit
from .context import Ctx
from .errors import DomainError

ACTIVE = ("active", "partial")


async def reference_price(ctx: Ctx) -> int:
    """Цена последнего торгового дня, а если его не было — последнего размещения."""
    for source in ("session", "placement"):
        row = (await ctx.s.execute(select(PriceHistory).where(PriceHistory.source == source)
                                   .order_by(PriceHistory.date.desc(), PriceHistory.id.desc()).limit(1))).scalar_one_or_none()
        if row:
            return row.price
    from .placements import last_placement_price
    return await last_placement_price(ctx) or await settings.get(ctx, "initial_reference_price_kop")


async def next_cutoff(ctx: Ctx, after=None):
    wd, hour = await settings.get_many(ctx, "cutoff_weekday", "cutoff_hour")
    return next_weekday_at(after or ctx.now, wd, time(hour, 0))


async def active_orders(ctx: Ctx, user_id: int) -> list[Order]:
    return list((await ctx.s.execute(select(Order).where(
        Order.user_id == user_id, Order.status.in_(ACTIVE)).order_by(Order.id))).scalars())


async def create(ctx: Ctx, user: User, side: str, shares: int, limit_price: int, weeks: int = 1) -> Order:
    """weeks=1 — до ближайшего торгового дня; до order_max_weeks недель."""
    users.require_member(user)
    if users.founder_side(user):
        raise DomainError("Основатель и связанные с ним аккаунты не торгуют на рынке между участниками")
    if user.banned_until and user.banned_until > ctx.now:
        raise DomainError("Торговля для вас временно запрещена из-за штрафных баллов")
    if side not in ("buy", "sell"):
        raise DomainError("Неизвестный тип заявки")
    if shares <= 0:
        raise DomainError("Количество SBAID должно быть положительным")
    max_weeks = await settings.get(ctx, "order_max_weeks")
    if not 1 <= weeks <= max_weeks:
        raise DomainError(f"Срок заявки — от 1 до {max_weeks} недель")
    # блокируем строку пользователя, чтобы параллельные заявки не обошли проверки
    await registry.lock_holdings(ctx, user.id)
    mine = await active_orders(ctx, user.id)
    opposite = [o for o in mine if o.side != side]
    if opposite:
        raise DomainError("Нельзя одновременно держать заявки на покупку и на продажу. "
                          "Сначала отмените другую заявку в /orders.")
    ref = await reference_price(ctx)
    band = await settings.get(ctx, "order_price_band_pct")
    lo, hi = max(100, ref * (100 - band) // 100), ref * (100 + band) // 100
    if not (lo <= limit_price <= hi):
        raise DomainError(f"Цена должна быть в пределах {rub(lo)} … {rub(hi)} "
                          f"(±{band}% от референсной {rub(ref)})")
    if side == "sell":
        if not user.payout_details_enc:
            raise DomainError("Сначала укажите, куда принимать оплату (телефон для СБП и банк)")
        await registry.block(ctx, user.id, shares)
    else:
        await registry.check_ownership_limit(ctx, user.id, shares)
    expires = await next_cutoff(ctx)
    expires += timedelta(weeks=weeks - 1)
    o = Order(user_id=user.id, side=side, shares=shares, limit_price=limit_price, created_at=ctx.now,
              expires_at=expires, filled=0, status="active")
    ctx.s.add(o)
    await ctx.s.flush()
    audit(ctx, user.id, "order.create", order_id=o.id, side=side, shares=shares, limit_price=limit_price,
          expires_at=expires)
    return o


async def cancel(ctx: Ctx, user: User, order_id: int) -> Order:
    o = (await ctx.s.execute(select(Order).where(Order.id == order_id).with_for_update())).scalar_one_or_none()
    if o is None or o.user_id != user.id:
        raise DomainError("Заявка не найдена")
    if o.status not in ACTIVE:
        raise DomainError("Заявка уже не активна")
    if o.side == "sell":
        await registry.unblock(ctx, user.id, o.remaining)
    o.status = "cancelled"
    audit(ctx, user.id, "order.cancel", order_id=o.id)
    return o
