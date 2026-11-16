"""Торговый день: отсечка, единая цена, сделки, итоги в канал."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import select

from ..models import Order, PriceHistory, Trade, TradeStatus, TradingSession, User
from ..timeutil import fmt_msk, next_weekday_at, rub
from . import registry, settings, trades as trades_svc
from .audit import audit
from .context import Ctx
from .errors import DomainError
from .matching import OrderIn, clear
from .orders import ACTIVE, reference_price
from .pricing import weighted_median


async def run(ctx: Ctx, cutoff_at: datetime, actor: User | None = None) -> TradingSession:
    """Провести торговый день с отсечкой cutoff_at. Повторный запуск с той же
    отсечкой не создаёт второй торговый день."""
    if actor is not None:
        from .users import require_founder
        require_founder(actor)
    existing = (await ctx.s.execute(select(TradingSession).where(
        TradingSession.cutoff_at == cutoff_at))).scalar_one_or_none()
    if existing:
        raise DomainError(f"Торговый день с отсечкой {fmt_msk(cutoff_at)} уже проведён")

    orders = (await ctx.s.execute(select(Order).where(
        Order.status.in_(ACTIVE), Order.created_at <= cutoff_at, Order.expires_at >= cutoff_at,
    ).order_by(Order.id).with_for_update())).scalars().all()
    ref = await reference_price(ctx)
    result = clear([OrderIn(o.id, o.user_id, o.side, o.remaining, o.limit_price, o.created_at) for o in orders], ref)

    sess = TradingSession(date=ctx.now, cutoff_at=cutoff_at, price=result.price, volume=result.volume,
                          trades_count=len(result.pairs), status="done" if result.happened else "failed")
    ctx.s.add(sess)
    await ctx.s.flush()
    audit(ctx, actor.id if actor else None, "session.run", session_id=sess.id, cutoff_at=cutoff_at,
          price=result.price, volume=result.volume, trades=len(result.pairs), status=sess.status,
          reference=ref)

    by_id = {o.id: o for o in orders}
    for oid, q in result.fills.items():
        o = by_id[oid]
        o.filled += q
        o.status = "filled" if o.remaining == 0 else "partial"

    pay_deadline, confirm_deadline = await _deadlines(ctx, cutoff_at)
    created: list[Trade] = []
    for pair in result.pairs:
        t = Trade(session_id=sess.id, buyer_id=pair.buyer_id, seller_id=pair.seller_id,
                  buy_order_id=pair.buy_order_id, sell_order_id=pair.sell_order_id, shares=pair.shares,
                  price=result.price, amount=pair.shares * result.price, pay_deadline=pay_deadline,
                  confirm_deadline=confirm_deadline, status=TradeStatus.AWAITING_PAYMENT,
                  reminders_sent=[], evidence=[])
        ctx.s.add(t)
        created.append(t)
    await ctx.s.flush()
    # акции продавца остаются заблокированными: блок заявки переходит на сделку

    # истёкшие заявки (срок — эта отсечка) снимаются, акции продавцов разблокируются
    for o in orders:
        if o.status in ACTIVE and o.expires_at <= cutoff_at:
            if o.side == "sell":
                await registry.unblock(ctx, o.user_id, o.remaining)
            o.status = "expired"
            ctx.send(o.user_id, f"Срок заявки №{o.id} истёк, неисполненный остаток {o.remaining} SBAID снят.")

    if result.happened:
        ctx.s.add(PriceHistory(date=ctx.now, source="session", price=result.price, volume=result.volume))
        for t in created:
            req = await trades_svc.seller_requisites(ctx, await ctx.s.get(User, t.buyer_id), t.id)
            text, kb = trades_svc.buyer_card(t, req)
            ctx.send(t.buyer_id, text, kb)
            ctx.send(t.seller_id, trades_svc.seller_card(t))
    await ctx.s.flush()
    median = await median_30d(ctx)
    if result.happened:
        ctx.publish(f"📊 Торговый день {fmt_msk(cutoff_at)}\nЕдиная цена: {rub(result.price)}\n"
                    f"Объём: {result.volume} SBAID, сделок: {len(result.pairs)}\n"
                    f"Медиана за 30 дней: {rub(median) if median else '—'}")
    else:
        ctx.publish(f"📊 Торговый день {fmt_msk(cutoff_at)} не состоялся: заявки не пересеклись. "
                    f"Референсная цена остаётся {rub(ref)}.")
    return sess


async def _deadlines(ctx: Ctx, cutoff_at: datetime) -> tuple[datetime, datetime]:
    pwd, ph, cwd, ch = await settings.get_many(ctx, "pay_weekday", "pay_hour", "confirm_weekday", "confirm_hour")
    pay = next_weekday_at(cutoff_at, pwd, time(ph, 0))
    confirm = next_weekday_at(pay, cwd, time(ch, 0))
    return pay, confirm


async def median_30d(ctx: Ctx) -> int | None:
    rows = (await ctx.s.execute(select(Trade.price, Trade.shares).where(
        Trade.status != TradeStatus.CANCELLED, Trade.id.in_(
            select(Trade.id).join(TradingSession, TradingSession.id == Trade.session_id)
            .where(TradingSession.cutoff_at >= ctx.now - timedelta(days=30)))))).all()
    return weighted_median([tuple(r) for r in rows]) if rows else None


async def last_cutoff(ctx: Ctx, now: datetime | None = None) -> datetime:
    """Последняя плановая отсечка, не позже now."""
    wd, hour = await settings.get_many(ctx, "cutoff_weekday", "cutoff_hour")
    now = now or ctx.now
    nxt = next_weekday_at(now, wd, time(hour, 0), inclusive=True)
    return nxt if nxt <= now else nxt - timedelta(days=7)
