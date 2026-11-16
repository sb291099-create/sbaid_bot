"""Заявки на рынке между участниками."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, text

from ..models import Order, PriceHistory, Trade, TradeStatus, User
from ..timeutil import fmt_msk, rub
from . import registry, settings, users
from .audit import audit
from .context import Ctx
from .errors import DomainError

ACTIVE = ("active", "partial")
BOOK_LOCK = 5_841_771  # номер advisory-блокировки стакана


async def reference_price(ctx: Ctx, before=None) -> int:
    """Текущая цена SBAID: цена последней сделки на бирже, а если сделок ещё не было —
    цена последнего размещения. before — цена на момент времени (для изменения за день)."""
    for source in ("trade", "placement"):
        q = select(PriceHistory.price).where(PriceHistory.source == source)
        if before is not None:
            q = q.where(PriceHistory.date < before)
        row = (await ctx.s.execute(q.order_by(PriceHistory.date.desc(), PriceHistory.id.desc()).limit(1))
               ).scalar_one_or_none()
        if row is not None:
            return row
    from .placements import last_placement_price
    return await last_placement_price(ctx) or await settings.get(ctx, "initial_reference_price_kop")


async def lock_book(ctx: Ctx) -> None:
    """Одна блокировка на весь стакан: заявки сводятся строго по очереди."""
    await ctx.s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": BOOK_LOCK})


async def active_orders(ctx: Ctx, user_id: int) -> list[Order]:
    return list((await ctx.s.execute(select(Order).where(
        Order.user_id == user_id, Order.status.in_(ACTIVE)).order_by(Order.id))).scalars())


async def create(ctx: Ctx, user: User, side: str, shares: int, limit_price: int, days: int = 7) -> Order:
    """Лимитная заявка. Сразу сводится со встречными заявками стакана: покупка — с
    самыми дешёвыми продажами, продажа — с самыми дорогими покупками, при равной
    цене раньше исполняется более ранняя заявка. Цена сделки — цена встречной
    заявки, которая уже стояла в стакане. Неисполненный остаток ждёт в стакане до
    конца срока."""
    users.require_member(user)
    if users.founder_side(user):
        raise DomainError("Основатель и связанные с ним аккаунты не торгуют на рынке между участниками")
    if user.banned_until and user.banned_until > ctx.now:
        raise DomainError("Торговля для вас временно запрещена из-за штрафных баллов")
    if side not in ("buy", "sell"):
        raise DomainError("Неизвестный тип заявки")
    if shares <= 0:
        raise DomainError("Количество SBAID должно быть положительным")
    max_days = await settings.get(ctx, "order_max_days")
    if not 1 <= days <= max_days:
        raise DomainError(f"Срок заявки — от 1 до {max_days} дней")
    await lock_book(ctx)
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
                          f"(±{band}% от цены последней сделки {rub(ref)})")
    if side == "sell":
        if not user.payout_details_enc:
            raise DomainError("Сначала укажите, куда принимать оплату (телефон для СБП и банк)")
        await registry.block(ctx, user.id, shares)
    else:
        await registry.check_ownership_limit(ctx, user.id, shares)
    expires = ctx.now + timedelta(days=days)
    o = Order(user_id=user.id, side=side, shares=shares, limit_price=limit_price, created_at=ctx.now,
              expires_at=expires, filled=0, status="active")
    ctx.s.add(o)
    await ctx.s.flush()
    audit(ctx, user.id, "order.create", order_id=o.id, side=side, shares=shares, limit_price=limit_price,
          expires_at=expires)
    o.new_trades = await _match(ctx, o)
    return o


async def _match(ctx: Ctx, taker: Order) -> list[Trade]:
    from . import trades as trades_svc
    if taker.side == "buy":
        cond = (Order.side == "sell", Order.limit_price <= taker.limit_price)
        best = (Order.limit_price.asc(), Order.created_at.asc(), Order.id.asc())
    else:
        cond = (Order.side == "buy", Order.limit_price >= taker.limit_price)
        best = (Order.limit_price.desc(), Order.created_at.asc(), Order.id.asc())
    makers = (await ctx.s.execute(select(Order).where(
        *cond, Order.status.in_(ACTIVE), Order.expires_at > ctx.now, Order.user_id != taker.user_id)
        .order_by(*best).with_for_update())).scalars().all()
    pay_h, conf_h = await settings.get_many(ctx, "trade_pay_hours", "trade_confirm_hours")
    made: list[Trade] = []
    for m in makers:
        if taker.remaining == 0:
            break
        q = min(taker.remaining, m.remaining)
        price = m.limit_price
        buy, sell = (taker, m) if taker.side == "buy" else (m, taker)
        pay = ctx.now + timedelta(hours=pay_h)
        t = Trade(buyer_id=buy.user_id, seller_id=sell.user_id, buy_order_id=buy.id, sell_order_id=sell.id,
                  shares=q, price=price, amount=q * price, pay_deadline=pay,
                  confirm_deadline=pay + timedelta(hours=conf_h), status=TradeStatus.AWAITING_PAYMENT,
                  reminders_sent=[], evidence=[], created_at=ctx.now)
        ctx.s.add(t)
        for o in (taker, m):
            o.filled += q
            o.status = "filled" if o.remaining == 0 else "partial"
        ctx.s.add(PriceHistory(date=ctx.now, source="trade", price=price, volume=q))
        await ctx.s.flush()
        # SBAID продавца остаются заблокированными: блок заявки переходит на сделку
        audit(ctx, None, "trade.create", trade_id=t.id, buy_order_id=buy.id, sell_order_id=sell.id,
              shares=q, price=price)
        buyer = await ctx.s.get(User, t.buyer_id)
        text_, kb = trades_svc.buyer_card(t, await trades_svc.seller_requisites(ctx, buyer, t.id))
        ctx.send(t.buyer_id, text_, kb)
        ctx.send(t.seller_id, trades_svc.seller_card(t))
        made.append(t)
    return made


async def expire(ctx: Ctx) -> int:
    """Снять заявки с истёкшим сроком; SBAID продавцов разблокируются."""
    await lock_book(ctx)
    rows = (await ctx.s.execute(select(Order).where(
        Order.status.in_(ACTIVE), Order.expires_at <= ctx.now).with_for_update())).scalars().all()
    for o in rows:
        if o.side == "sell":
            await registry.unblock(ctx, o.user_id, o.remaining)
        o.status = "expired"
        audit(ctx, None, "order.expire", order_id=o.id)
        ctx.send(o.user_id, f"Срок заявки №{o.id} истёк ({fmt_msk(o.expires_at)}), неисполненный остаток "
                            f"{o.remaining} SBAID снят из стакана.")
    return len(rows)


async def cancel(ctx: Ctx, user: User, order_id: int) -> Order:
    await lock_book(ctx)
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
