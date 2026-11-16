"""Биржа SBAID: котировка, стакан, ежедневная сводка в канал."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import func, select

from ..models import AppState, Order, PriceHistory, Trade, TradeStatus
from ..timeutil import MSK, fmt_msk, rub
from . import settings
from .context import Ctx
from .orders import ACTIVE, reference_price
from .pricing import weighted_median


@dataclass
class Level:
    price: int
    shares: int
    orders: int


@dataclass
class Quote:
    last: int
    prev_close: int
    best_bid: int | None
    best_ask: int | None
    trades_24h: int
    volume_24h: int

    @property
    def change_pct(self) -> float:
        return (self.last - self.prev_close) * 100 / self.prev_close if self.prev_close else 0.0


def day_start(now: datetime) -> datetime:
    """Начало текущих суток по Москве."""
    return datetime.combine(now.astimezone(MSK).date(), time(0, 0), tzinfo=MSK)


async def book(ctx: Ctx, depth: int = 5) -> tuple[list[Level], list[Level]]:
    """(продажи от дешёвых, покупки от дорогих), сгруппированные по цене."""
    out = []
    for side, order in (("sell", Order.limit_price.asc()), ("buy", Order.limit_price.desc())):
        rows = (await ctx.s.execute(select(
            Order.limit_price, func.sum(Order.shares - Order.filled), func.count(Order.id)).where(
            Order.side == side, Order.status.in_(ACTIVE), Order.expires_at > ctx.now)
            .group_by(Order.limit_price).order_by(order).limit(depth))).all()
        out.append([Level(int(p), int(q), int(n)) for p, q, n in rows])
    return out[0], out[1]


async def quote(ctx: Ctx) -> Quote:
    last = await reference_price(ctx)
    prev = await reference_price(ctx, before=day_start(ctx.now))
    asks, bids = await book(ctx, 1)
    cnt, vol = (await ctx.s.execute(select(func.count(Trade.id), func.coalesce(func.sum(Trade.shares), 0)).where(
        Trade.created_at > ctx.now - timedelta(hours=24)))).one()
    return Quote(last=last, prev_close=prev, best_bid=bids[0].price if bids else None,
                 best_ask=asks[0].price if asks else None, trades_24h=int(cnt), volume_24h=int(vol))


def quote_line(q: Quote) -> str:
    sign = "+" if q.change_pct > 0 else ""
    return f"SBAID {rub(q.last)} ({sign}{q.change_pct:.1f}% за сегодня)"


async def market_text(ctx: Ctx, depth: int = 5) -> str:
    q = await quote(ctx)
    asks, bids = await book(ctx, depth)
    lines = [f"📈 {quote_line(q)}",
             f"Сделок за 24 ч: {q.trades_24h}, объём {q.volume_24h} SBAID", "",
             "Продают:"]
    lines += [f"  {rub(l.price)} — {l.shares} SBAID" for l in reversed(asks)] or ["  —"]
    lines.append("Покупают:")
    lines += [f"  {rub(l.price)} — {l.shares} SBAID" for l in bids] or ["  —"]
    lines += ["", "Купить сразу: /buy с ценой не ниже лучшей продажи. Продать сразу: /sell с ценой "
                  "не выше лучшей покупки."]
    return "\n".join(lines)


async def median_30d(ctx: Ctx) -> int | None:
    rows = (await ctx.s.execute(select(Trade.price, Trade.shares).where(
        Trade.status == TradeStatus.COMPLETED, Trade.completed_at >= ctx.now - timedelta(days=30)))).all()
    return weighted_median([tuple(r) for r in rows]) if rows else None


async def daily_summary(ctx: Ctx) -> bool:
    """Сводка торгов за сутки в канал, один раз в день в summary_hour МСК.
    Если за день не было сделок, сводка не публикуется."""
    hour = await settings.get(ctx, "summary_hour")
    local = ctx.now.astimezone(MSK)
    if local.hour < hour:
        return False
    key = f"summary:{local.date().isoformat()}"
    if await ctx.s.get(AppState, key):
        return False
    ctx.s.add(AppState(key=key, value=ctx.now.isoformat()))
    start = day_start(ctx.now)
    rows = (await ctx.s.execute(select(Trade.price, Trade.shares).where(
        Trade.created_at >= start).order_by(Trade.id))).all()
    if not rows:
        return False
    q = await quote(ctx)
    prices = [p for p, _ in rows]
    median = await median_30d(ctx)
    ctx.publish(f"📊 Биржа SBAID за {local.strftime('%d.%m.%Y')}\n"
                f"Цена: {rub(q.last)} ({'+' if q.change_pct > 0 else ''}{q.change_pct:.1f}% за день)\n"
                f"Мин. {rub(min(prices))}, макс. {rub(max(prices))}\n"
                f"Сделок: {len(rows)}, объём {sum(s for _, s in rows)} SBAID\n"
                f"Лучшая покупка: {rub(q.best_bid) if q.best_bid else '—'}, "
                f"лучшая продажа: {rub(q.best_ask) if q.best_ask else '—'}\n"
                f"Медиана завершённых сделок за 30 дней: {rub(median) if median else '—'}\n"
                f"Время публикации: {fmt_msk(ctx.now)}")
    return True
