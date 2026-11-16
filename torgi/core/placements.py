"""Размещения: создание, публикация, закрытие, резерв и оплата через Robokassa."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..models import Placement, PlacementBid, PriceHistory, Purchase, Role, Trade, TradeStatus, User
from ..timeutil import fmt_msk, rub
from . import registry, settings, users
from .audit import audit
from .context import Button, Ctx
from .errors import DomainError
from .pricing import PriceCalc, ladder_amount, ladder_price, next_placement_price


def unit_price(p: Placement, index: int) -> int:
    """Цена SBAID с порядковым номером index (с нуля) в размещении p."""
    return ladder_price(p.price, index, p.ladder_every or 0, p.ladder_step or 0)


def final_price(p: Placement) -> int:
    """Цена последнего проданного SBAID — с неё стартует следующее размещение, если сделок мало."""
    return unit_price(p, max(0, p.sold - 1))


def ladder_text(p: Placement) -> str:
    if p.mode == "auction":
        floor = f" Минимальная цена заявки {rub(p.min_price)}." if p.min_price else ""
        return (f"Аукцион: до окончания приёма заявок каждый указывает, сколько SBAID и по какой цене "
                f"готов купить. Затем бот находит одну цену, при которой расходится партия, и все "
                f"победители платят по ней. Рыночный ориентир — {rub(p.price)}.{floor}")
    if not p.ladder_every or not p.ladder_step:
        return f"Цена {rub(p.price)} за SBAID на всю партию."
    top = unit_price(p, p.volume - 1)
    return (f"Цена растёт лесенкой: первые {p.ladder_every} SBAID по {rub(p.price)}, каждые следующие "
            f"{p.ladder_every} дороже на {rub(p.ladder_step)}, последние — по {rub(top)}.")


async def last_placement_price(ctx: Ctx) -> int | None:
    p = (await ctx.s.execute(select(Placement).where(Placement.status.in_(("open", "closed")))
                             .order_by(Placement.number.desc()).limit(1))).scalar_one_or_none()
    return final_price(p) if p else None


async def next_unit_price(ctx: Ctx, p: Placement) -> int:
    return unit_price(p, p.sold + await _reserved(ctx, p.id))


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
                 starts_at: datetime | None = None, mode: str = "fixed") -> Placement:
    """mode=fixed: price — цена (None — расчётная рыночная).
    mode=auction: price — минимальная цена заявки (None — без минимума), цену задаст аукцион."""
    users.require_founder(actor)
    if volume <= 0 or days <= 0:
        raise DomainError("Объём и срок должны быть положительными")
    if mode not in ("fixed", "auction"):
        raise DomainError("Неизвестный способ размещения")
    calc = await suggested_price(ctx)
    min_price = 0
    if mode == "auction":
        min_price = price or 0
        price = calc.price  # ориентир; итоговую цену задаст аукцион
    elif price is None:
        price = calc.price
    if price <= 0 or min_price < 0:
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
    lot, limit_pct, every, lstep = await settings.get_many(
        ctx, "placement_lot", "placement_user_limit_pct", "ladder_every", "ladder_step_kop")
    per_user = max(lot, (volume * limit_pct // 100) // lot * lot)
    number = int((await ctx.s.execute(select(func.coalesce(func.max(Placement.number), 0)))).scalar_one()) + 1
    starts_at = starts_at or ctx.now
    if mode == "auction":
        every = lstep = 0
    p = Placement(number=number, volume=volume, price=price, mode=mode, min_price=min_price,
                  ladder_every=every, ladder_step=lstep,
                  starts_at=starts_at,
                  ends_at=starts_at + timedelta(days=days), lot=lot, per_user_limit=per_user, status="draft",
                  price_calc={"suggested": calc.price, "steps": calc.steps, "used": price})
    ctx.s.add(p)
    await ctx.s.flush()
    audit(ctx, actor.id, "placement.create", placement_id=p.id, volume=volume, price=price, mode=mode,
          min_price=min_price, suggested=calc.price, ends_at=p.ends_at)
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
    ctx.publish(f"📢 Размещение №{p.number}\nОбъём: {p.volume} SBAID\n{ladder_text(p)}\n"
                f"Лот: {p.lot}, лимит в одни руки: {p.per_user_limit}\nДо {fmt_msk(p.ends_at)}\n\n"
                f"Стартовая цена:\n{steps}{note}\n\nКупить: /placement в боте")
    return p


async def close(ctx: Ctx, actor: User | None, placement_id: int) -> Placement:
    if actor is not None:
        users.require_founder(actor)
    p = await _get_locked(ctx, placement_id)
    if p.status != "open":
        raise DomainError("Размещение не открыто")
    p.status = "closed"
    if p.mode == "auction":
        await _finish_auction(ctx, p, actor)
        return p
    reserved = (await ctx.s.execute(select(Purchase).where(
        Purchase.placement_id == p.id, Purchase.status == "reserved").with_for_update())).scalars().all()
    for r in reserved:
        r.status = "expired"
    if p.sold:
        ctx.s.add(PriceHistory(date=ctx.now, source="placement", price=final_price(p), volume=p.sold))
    audit(ctx, actor.id if actor else None, "placement.close", placement_id=p.id, sold=p.sold,
          returned_to_treasury=p.volume - p.sold)
    buyers = (await ctx.s.execute(select(func.count(func.distinct(Purchase.user_id))).where(
        Purchase.placement_id == p.id, Purchase.status == "paid"))).scalar_one()
    revenue = int((await ctx.s.execute(select(func.coalesce(func.sum(Purchase.amount), 0)).where(
        Purchase.placement_id == p.id, Purchase.status == "paid"))).scalar_one())
    prices = (f"по {rub(p.price)}" if final_price(p) == p.price
              else f"по цене от {rub(p.price)} до {rub(final_price(p))}")
    avg = f", средняя {rub(revenue // p.sold)}" if p.sold and final_price(p) != p.price else ""
    ctx.publish(f"✅ Размещение №{p.number} завершено\nПродано {p.sold} из {p.volume} SBAID {prices}{avg}, "
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
    if cur.mode == "auction":
        raise DomainError("Сейчас идёт аукцион: подайте заявку через /placement")
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
    # место в очереди: проданные + чужие активные резервы; цена каждой SBAID — по её номеру
    start = p.sold + await _reserved(ctx, p.id)
    amount = ladder_amount(p.price, start, shares, p.ladder_every or 0, p.ladder_step or 0)
    pur = Purchase(placement_id=p.id, user_id=user.id, shares=shares, amount=amount, status="reserved",
                   reserved_until=ctx.now + timedelta(minutes=minutes), shares_remaining=0)
    ctx.s.add(pur)
    await ctx.s.flush()
    audit(ctx, user.id, "purchase.reserve", purchase_id=pur.id, shares=shares, amount=amount)
    url = payment_link(ctx, pur)
    if not payments_configured(ctx):
        ctx.staff(f"🧪 Пробный режим: {user.display} зарезервировал {shares} SBAID, счёт №{pur.id} на "
                  f"{rub(amount)}. Подтвердить оплату вручную: /testpay {pur.id}")
    return pur, url


def payments_configured(ctx: Ctx) -> bool:
    rk = ctx.app.payments
    return rk is not None and rk.configured


async def test_payment(ctx: Ctx, actor: User, purchase_id: int) -> str:
    """Пробный режим: основатель вручную подтверждает оплату, пока Robokassa не подключена.
    После подключения Robokassa команда отключается."""
    users.require_founder(actor)
    if payments_configured(ctx):
        raise DomainError("Robokassa подключена — оплата подтверждается только её уведомлением")
    pur = await ctx.s.get(Purchase, purchase_id)
    if pur is None:
        raise DomainError("Счёт не найден")
    audit(ctx, actor.id, "purchase.test_payment", purchase_id=purchase_id, amount=pur.amount)
    return await on_payment(ctx, purchase_id, pur.amount, op="test")


def payment_link(ctx: Ctx, pur: Purchase) -> str:
    rk = ctx.app.payments
    name = ctx.config.receipt_item_name.format(n=pur.shares)
    if rk is None or not rk.configured:
        return (f"Бот работает в пробном режиме, онлайн-оплата ещё не подключена. Ваш счёт №{pur.id} "
                f"на {rub(pur.amount)}: основатель подтвердит оплату вручную.")
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
    ctx.send(pur.user_id, f"✅ Оплата получена. Вам начислено {pur.shares} SBAID на {rub(pur.amount)}.\n"
                          f"Портфель: /portfolio")
    u = await users.get(ctx, pur.user_id)
    if not u.group_invite_sent:
        u.group_invite_sent = True
        ctx.invite(u.id)
    if p.sold >= p.volume and p.status == "open":
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


# --- аукцион размещения ----------------------------------------------------------------

def clear_auction(volume: int, lot: int, bids: list[PlacementBid]) -> tuple[int | None, dict[int, int]]:
    """Аукцион с единой ценой. Заявки по убыванию цены (при равной — раньше поданная)
    получают SBAID, пока не кончится партия; цена для всех — цена последней
    удовлетворённой заявки. Возвращает (цена, {bid_id: SBAID})."""
    left, alloc, price = volume, {}, None
    for b in sorted(bids, key=lambda b: (-b.max_price, b.created_at, b.id)):
        if left < lot:
            break
        q = min(b.shares, left) // lot * lot
        if q <= 0:
            continue
        alloc[b.id] = q
        left -= q
        price = b.max_price
    return price, alloc


async def my_bid(ctx: Ctx, placement_id: int, user_id: int) -> PlacementBid | None:
    return (await ctx.s.execute(select(PlacementBid).where(
        PlacementBid.placement_id == placement_id, PlacementBid.user_id == user_id,
        PlacementBid.status == "active"))).scalar_one_or_none()


async def _open_auction(ctx: Ctx) -> Placement:
    cur = await current(ctx)
    if cur is None or cur.mode != "auction" or cur.ends_at <= ctx.now:
        raise DomainError("Сейчас нет открытого аукциона размещения")
    return await _get_locked(ctx, cur.id)


async def place_bid(ctx: Ctx, user: User, shares: int, max_price: int) -> PlacementBid:
    """Подать или заменить заявку на аукционе. Деньги не списываются до итогов."""
    if user.role == Role.FOUNDER:
        raise DomainError("Основатель не участвует в аукционе — его SBAID и так в казне")
    users.require_member(user)
    p = await _open_auction(ctx)
    if shares <= 0 or shares % p.lot:
        raise DomainError(f"Количество должно быть кратно лоту ({p.lot} SBAID)")
    if shares > p.per_user_limit:
        raise DomainError(f"Лимит в одни руки — {p.per_user_limit} SBAID")
    if max_price < max(100, p.min_price):
        raise DomainError(f"Цена заявки — не меньше {rub(max(100, p.min_price))}")
    old = await my_bid(ctx, p.id, user.id)
    if old is not None:
        old.status = "cancelled"
        await ctx.s.flush()
    await registry.check_ownership_limit(ctx, user.id, shares)
    b = PlacementBid(placement_id=p.id, user_id=user.id, shares=shares, max_price=max_price,
                     created_at=ctx.now, status="active", allocated=0)
    ctx.s.add(b)
    await ctx.s.flush()
    audit(ctx, user.id, "auction.bid", placement_id=p.id, bid_id=b.id, shares=shares, max_price=max_price,
          replaced=old.id if old else None)
    return b


async def cancel_bid(ctx: Ctx, user: User) -> None:
    p = await _open_auction(ctx)
    b = await my_bid(ctx, p.id, user.id)
    if b is None:
        raise DomainError("У вас нет активной заявки")
    b.status = "cancelled"
    audit(ctx, user.id, "auction.cancel", placement_id=p.id, bid_id=b.id)


async def _finish_auction(ctx: Ctx, p: Placement, actor: User | None) -> None:
    bids = (await ctx.s.execute(select(PlacementBid).where(
        PlacementBid.placement_id == p.id, PlacementBid.status == "active").with_for_update())).scalars().all()
    price, alloc = clear_auction(p.volume, p.lot, list(bids))
    demand = sum(b.shares for b in bids)
    hours = await settings.get(ctx, "auction_pay_hours")
    pay_until = ctx.now + timedelta(hours=hours)
    if price is not None:
        p.price = price
    allocated = sum(alloc.values())
    for b in bids:
        q = alloc.get(b.id, 0)
        b.allocated = q
        b.status = "won" if q == b.shares else ("partial" if q else "lost")
        if not q:
            ctx.send(b.user_id, f"Аукцион размещения №{p.number} завершён по цене {rub(price)}. "
                                f"Ваша заявка по {rub(b.max_price)} оказалась ниже цены отсечения, "
                                f"SBAID не достались. Деньги не списывались.")
            continue
        pur = Purchase(placement_id=p.id, user_id=b.user_id, shares=q, amount=q * price, status="reserved",
                       reserved_until=pay_until, shares_remaining=0)
        ctx.s.add(pur)
        await ctx.s.flush()
        audit(ctx, None, "purchase.reserve", purchase_id=pur.id, shares=q, amount=pur.amount, auction_bid=b.id)
        part = "" if q == b.shares else f" (из {b.shares} запрошенных: партия закончилась)"
        url = payment_link(ctx, pur)
        kb = [[Button(f"Оплатить {rub(pur.amount)}", url=url)]] if url.startswith("http") else None
        ctx.send(b.user_id, f"🎉 Аукцион размещения №{p.number} завершён. Цена для всех: {rub(price)}.\n"
                            f"Вам достаётся {q} SBAID{part} на {rub(pur.amount)}. Оплатите до "
                            f"{fmt_msk(pay_until)}, иначе резерв снимется." + ("" if kb else f"\n{url}"), kb)
        if not payments_configured(ctx):
            ctx.staff(f"🧪 Пробный режим: счёт №{pur.id} на {rub(pur.amount)} ({q} SBAID). /testpay {pur.id}")
    if price is not None:
        ctx.s.add(PriceHistory(date=ctx.now, source="placement", price=price, volume=allocated))
    audit(ctx, actor.id if actor else None, "placement.close", placement_id=p.id, mode="auction",
          price=price, demand=demand, allocated=allocated, bids=len(bids))
    if price is None:
        ctx.publish(f"Аукцион размещения №{p.number} завершён без заявок. Все {p.volume} SBAID остаются в казне.")
    else:
        ctx.publish(f"✅ Аукцион размещения №{p.number} завершён\nЦена отсечения: {rub(price)}\n"
                    f"Спрос: {demand} SBAID от {len(bids)} участников, распределено {allocated} из {p.volume}.\n"
                    f"Победители оплачивают до {fmt_msk(pay_until)}; неоплаченное и нераспределённое "
                    f"остаётся в казне.")
