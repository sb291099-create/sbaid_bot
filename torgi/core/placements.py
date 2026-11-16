"""Размещения: создание, публикация, закрытие, резерв и оплата через Robokassa."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..models import Placement, PriceHistory, Purchase, Role, Trade, TradeStatus, User
from ..timeutil import fmt_msk, rub
from . import registry, settings, users
from .audit import audit
from .context import Ctx
from .errors import DomainError
from .pricing import PriceCalc, next_placement_price


async def last_placement_price(ctx: Ctx) -> int | None:
    p = (await ctx.s.execute(select(Placement).where(Placement.status.in_(("open", "closed")))
                             .order_by(Placement.number.desc()).limit(1))).scalar_one_or_none()
    return p.price if p else None


async def suggested_price(ctx: Ctx) -> PriceCalc:
    (window, min_trades, min_buyers, down, up, floor, step, initial) = await settings.get_many(
        ctx, "price_window_days", "price_min_trades", "price_min_buyers", "price_corridor_down_pct",
        "price_corridor_up_pct", "price_floor_kop", "price_step_kop", "initial_reference_price_kop")
    prev = await last_placement_price(ctx)
    if prev is None:
        return PriceCalc(price=initial, steps=["Первое размещение — цену задаёт основатель"])
    rows = (await ctx.s.execute(select(Trade.price, Trade.shares, Trade.buyer_id).where(
        Trade.status == TradeStatus.COMPLETED,
        Trade.completed_at >= ctx.now - timedelta(days=window)))).all()
    return next_placement_price(prev, [tuple(r) for r in rows], min_trades=min_trades, min_buyers=min_buyers,
                                down_pct=down, up_pct=up, floor=floor, step=step)


async def create(ctx: Ctx, actor: User, volume: int, price: int | None, days: int,
                 starts_at: datetime | None = None) -> Placement:
    users.require_founder(actor)
    if volume <= 0 or days <= 0:
        raise DomainError("Объём и срок должны быть положительными")
    calc = await suggested_price(ctx)
    if price is None:
        price = calc.price
    if price <= 0:
        raise DomainError("Цена должна быть положительной")
    t = await registry.treasury(ctx)
    tre = await registry.holding(ctx, t.id)
    busy = (await ctx.s.execute(select(func.coalesce(func.sum(Placement.volume - Placement.sold), 0)).where(
        Placement.status.in_(("draft", "open"))))).scalar_one()
    if volume > tre.total - int(busy):
        raise DomainError(f"В казне свободно только {tre.total - int(busy)} SBAID")
    cap, period = await settings.get_many(ctx, "placement_period_cap", "placement_period_days")
    recent = int((await ctx.s.execute(select(func.coalesce(func.sum(Placement.volume), 0)).where(
        Placement.status.in_(("draft", "open", "closed")),
        Placement.starts_at > (starts_at or ctx.now) - timedelta(days=period)))).scalar_one())
    if recent + volume > cap:
        raise DomainError(f"За {period} дней можно разместить не больше {cap} SBAID; "
                          f"уже размещено {recent}, доступно {max(0, cap - recent)}")
    lot, limit_pct = await settings.get_many(ctx, "placement_lot", "placement_user_limit_pct")
    per_user = max(lot, (volume * limit_pct // 100) // lot * lot)
    number = int((await ctx.s.execute(select(func.coalesce(func.max(Placement.number), 0)))).scalar_one()) + 1
    starts_at = starts_at or ctx.now
    p = Placement(number=number, volume=volume, price=price, starts_at=starts_at,
                  ends_at=starts_at + timedelta(days=days), lot=lot, per_user_limit=per_user, status="draft",
                  price_calc={"suggested": calc.price, "steps": calc.steps, "used": price})
    ctx.s.add(p)
    await ctx.s.flush()
    audit(ctx, actor.id, "placement.create", placement_id=p.id, volume=volume, price=price,
          suggested=calc.price, ends_at=p.ends_at)
    return p


async def publish(ctx: Ctx, actor: User, placement_id: int) -> Placement:
    users.require_founder(actor)
    p = await _get_locked(ctx, placement_id)
    if p.status != "draft":
        raise DomainError("Опубликовать можно только черновик")
    if (await current(ctx)) is not None:
        raise DomainError("Уже идёт другое размещение")
    p.status = "open"
    audit(ctx, actor.id, "placement.publish", placement_id=p.id)
    calc = p.price_calc or {}
    steps = "\n".join(f"• {s}" for s in calc.get("steps", []))
    note = "" if calc.get("used") == calc.get("suggested") else \
        f"\nОснователь установил цену {rub(p.price)} (расчётная — {rub(calc.get('suggested', 0))})."
    ctx.publish(f"📢 Размещение №{p.number}\nОбъём: {p.volume} SBAID по {rub(p.price)}\n"
                f"Лот: {p.lot}, лимит в одни руки: {p.per_user_limit}\nДо {fmt_msk(p.ends_at)}\n\n"
                f"Расчёт цены:\n{steps}{note}\n\nКупить: /placement в боте")
    return p


async def close(ctx: Ctx, actor: User | None, placement_id: int) -> Placement:
    if actor is not None:
        users.require_founder(actor)
    p = await _get_locked(ctx, placement_id)
    if p.status != "open":
        raise DomainError("Размещение не открыто")
    p.status = "closed"
    reserved = (await ctx.s.execute(select(Purchase).where(
        Purchase.placement_id == p.id, Purchase.status == "reserved").with_for_update())).scalars().all()
    for r in reserved:
        r.status = "expired"
    if p.sold:
        ctx.s.add(PriceHistory(date=ctx.now, source="placement", price=p.price, volume=p.sold))
    audit(ctx, actor.id if actor else None, "placement.close", placement_id=p.id, sold=p.sold,
          returned_to_treasury=p.volume - p.sold)
    buyers = (await ctx.s.execute(select(func.count(func.distinct(Purchase.user_id))).where(
        Purchase.placement_id == p.id, Purchase.status == "paid"))).scalar_one()
    ctx.publish(f"✅ Размещение №{p.number} завершено\nПродано {p.sold} из {p.volume} SBAID по {rub(p.price)}, "
                f"покупателей: {buyers}.\nНепроданные {p.volume - p.sold} SBAID остаются в казне.")
    return p


async def close_expired(ctx: Ctx) -> None:
    rows = (await ctx.s.execute(select(Placement.id).where(
        Placement.status == "open", Placement.ends_at <= ctx.now))).scalars().all()
    for pid in rows:
        await close(ctx, None, pid)


async def current(ctx: Ctx) -> Placement | None:
    return (await ctx.s.execute(select(Placement).where(Placement.status == "open").limit(1))).scalar_one_or_none()


async def _get_locked(ctx: Ctx, placement_id: int) -> Placement:
    p = (await ctx.s.execute(select(Placement).where(Placement.id == placement_id).with_for_update())).scalar_one_or_none()
    if p is None:
        raise DomainError("Размещение не найдено")
    return p


async def _reserved(ctx: Ctx, placement_id: int, user_id: int | None = None) -> int:
    q = select(func.coalesce(func.sum(Purchase.shares), 0)).where(
        Purchase.placement_id == placement_id, Purchase.status == "reserved")
    if user_id is not None:
        q = q.where(Purchase.user_id == user_id)
    return int((await ctx.s.execute(q)).scalar_one())


async def available(ctx: Ctx, p: Placement) -> int:
    return p.volume - p.sold - await _reserved(ctx, p.id)


async def user_bought(ctx: Ctx, placement_id: int, user_id: int) -> int:
    return int((await ctx.s.execute(select(func.coalesce(func.sum(Purchase.shares), 0)).where(
        Purchase.placement_id == placement_id, Purchase.user_id == user_id,
        Purchase.status.in_(("reserved", "paid"))))).scalar_one())


async def reserve(ctx: Ctx, user: User, shares: int) -> tuple[Purchase, str]:
    """Резерв акций на время оплаты и ссылка на оплату."""
    users.require_member(user)
    if user.role == Role.FOUNDER:
        raise DomainError("Основатель не покупает на размещении — его SBAID и так в казне")
    cur = await current(ctx)
    if cur is None or cur.ends_at <= ctx.now:
        raise DomainError("Сейчас нет открытого размещения")
    p = await _get_locked(ctx, cur.id)
    if shares <= 0 or shares % p.lot:
        raise DomainError(f"Количество должно быть кратно лоту ({p.lot} SBAID)")
    mine = await user_bought(ctx, p.id, user.id)
    if mine + shares > p.per_user_limit:
        raise DomainError(f"Лимит в одни руки — {p.per_user_limit} SBAID, у вас уже {mine}")
    left = await available(ctx, p)
    if shares > left:
        raise DomainError(f"Осталось только {left} SBAID")
    await registry.check_ownership_limit(ctx, user.id, shares)
    minutes = await settings.get(ctx, "reserve_minutes")
    amount = shares * p.price
    pur = Purchase(placement_id=p.id, user_id=user.id, shares=shares, amount=amount, status="reserved",
                   reserved_until=ctx.now + timedelta(minutes=minutes), shares_remaining=0)
    ctx.s.add(pur)
    await ctx.s.flush()
    audit(ctx, user.id, "purchase.reserve", purchase_id=pur.id, shares=shares, amount=amount)
    url = payment_link(ctx, pur)
    return pur, url


def payment_link(ctx: Ctx, pur: Purchase) -> str:
    rk = ctx.app.payments
    name = ctx.config.receipt_item_name.format(n=pur.shares)
    if rk is None or not rk.configured:
        return f"(Robokassa не настроена; счёт №{pur.id} на {rub(pur.amount)})"
    return rk.payment_url(pur.id, pur.amount, name, rk.receipt(name, pur.shares, pur.amount))


async def on_payment(ctx: Ctx, inv_id: int, amount: int, op: str | None = None) -> str:
    """Уведомление об оплате. Повторное уведомление ничего не начисляет.
    Возвращает статус обработки (для логов и тестов)."""
    pur = (await ctx.s.execute(select(Purchase).where(Purchase.id == inv_id).with_for_update())).scalar_one_or_none()
    if pur is None:
        audit(ctx, None, "payment.unknown_invoice", inv_id=inv_id, amount=amount)
        return "unknown"
    if pur.status in ("paid", "paid_late"):
        return "duplicate"
    if amount != pur.amount:
        audit(ctx, None, "payment.amount_mismatch", purchase_id=pur.id, expected=pur.amount, got=amount)
        ctx.staff(f"❗ Оплата счёта №{pur.id}: сумма {rub(amount)} не совпадает с {rub(pur.amount)}. Проверьте вручную.")
        return "mismatch"
    p = await _get_locked(ctx, pur.placement_id)
    if pur.status == "expired":
        # резерв уже снят — начисляем, только если акции ещё есть
        if p.status != "open" or await available(ctx, p) < pur.shares:
            pur.status = "paid_late"
            pur.paid_at = ctx.now
            audit(ctx, None, "purchase.paid_late", purchase_id=pur.id)
            ctx.staff(f"❗ Счёт №{pur.id} оплачен после снятия резерва, SBAID не осталось. Нужен возврат "
                      f"{rub(pur.amount)} пользователю id{pur.user_id}.")
            ctx.send(pur.user_id, "Оплата пришла после окончания резерва, и SBAID уже не осталось. "
                                  "Основатель вернёт деньги, мы сообщим.")
            return "late"
    t = await registry.treasury(ctx)
    await registry.transfer(ctx, t.id, pur.user_id, pur.shares, "placement", f"purchase:{pur.id}",
                            consume_lots=False)
    pur.status, pur.paid_at, pur.robokassa_op = "paid", ctx.now, op
    pur.shares_remaining = pur.shares
    p.sold += pur.shares
    audit(ctx, None, "purchase.paid", purchase_id=pur.id, shares=pur.shares, amount=pur.amount)
    ctx.send(pur.user_id, f"✅ Оплата получена. Вам начислено {pur.shares} SBAID по {rub(p.price)}.\n"
                          f"Портфель: /portfolio")
    u = await users.get(ctx, pur.user_id)
    if not u.group_invite_sent:
        u.group_invite_sent = True
        ctx.invite(u.id)
    if p.sold >= p.volume:
        await close(ctx, None, p.id)
    return "credited"


async def expire_reservations(ctx: Ctx) -> int:
    rows = (await ctx.s.execute(select(Purchase).where(
        Purchase.status == "reserved", Purchase.reserved_until <= ctx.now).with_for_update())).scalars().all()
    for r in rows:
        r.status = "expired"
        audit(ctx, None, "purchase.expired", purchase_id=r.id)
        ctx.send(r.user_id, f"Резерв {r.shares} SBAID снят: оплата не пришла вовремя.")
    return len(rows)
