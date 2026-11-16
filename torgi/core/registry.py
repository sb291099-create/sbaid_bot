"""Реестр акций: перенос акций, блокировки, доли и лимит владения.

Любое движение акций идёт через transfer(): одна транзакция, блокировка
строк holdings (SELECT … FOR UPDATE в порядке id, чтобы не было взаимных
блокировок) и запись в неизменяемый журнал transfers.
"""
from __future__ import annotations

from sqlalchemy import func, select

from ..models import (
    Gift, Holding, Order, Placement, Purchase, Role, Trade, TradeStatus, Transfer, User,
)
from . import settings
from .context import Ctx
from .errors import DomainError


async def treasury(ctx: Ctx) -> User:
    return (await ctx.s.execute(select(User).where(User.role == Role.TREASURY))).scalar_one()


async def holding(ctx: Ctx, user_id: int, lock: bool = False) -> Holding:
    q = select(Holding).where(Holding.user_id == user_id)
    if lock:
        q = q.with_for_update()
    h = (await ctx.s.execute(q)).scalar_one_or_none()
    if h is None:
        h = Holding(user_id=user_id, total=0, blocked=0)
        ctx.s.add(h)
        await ctx.s.flush()
        if lock:
            h = (await ctx.s.execute(q)).scalar_one()
    return h


async def lock_holdings(ctx: Ctx, *user_ids: int) -> dict[int, Holding]:
    out = {}
    for uid in sorted(set(user_ids)):
        out[uid] = await holding(ctx, uid, lock=True)
    return out


async def block(ctx: Ctx, user_id: int, shares: int) -> None:
    h = (await lock_holdings(ctx, user_id))[user_id]
    if h.total - h.blocked < shares:
        raise DomainError(f"Недостаточно свободных акций: доступно {h.total - h.blocked}")
    h.blocked += shares


async def unblock(ctx: Ctx, user_id: int, shares: int) -> None:
    h = (await lock_holdings(ctx, user_id))[user_id]
    h.blocked -= shares
    if h.blocked < 0:
        raise RuntimeError("blocked < 0 — нарушена целостность реестра")


async def transfer(ctx: Ctx, from_id: int, to_id: int, shares: int, type_: str, basis: str,
                   *, from_blocked: bool = False, consume_lots: bool = True) -> Transfer:
    """Перенести акции. from_blocked — акции уже заблокированы у отправителя
    (под заявку, сделку, подарок) и снимаются из заблокированных."""
    if shares <= 0:
        raise DomainError("Количество акций должно быть положительным")
    if from_id == to_id:
        raise DomainError("Нельзя переносить акции самому себе")
    hs = await lock_holdings(ctx, from_id, to_id)
    src, dst = hs[from_id], hs[to_id]
    if from_blocked:
        if src.blocked < shares:
            raise RuntimeError("Заблокировано меньше, чем переносится")
        src.blocked -= shares
    elif src.total - src.blocked < shares:
        raise DomainError(f"Недостаточно свободных акций: доступно {src.total - src.blocked}")
    src.total -= shares
    dst.total += shares
    t = Transfer(from_user_id=from_id, to_user_id=to_id, shares=shares, type=type_, basis=basis,
                 created_at=ctx.now)
    ctx.s.add(t)
    if consume_lots:
        await _consume_placement_lots(ctx, from_id, shares)
    await ctx.s.flush()
    return t


async def _consume_placement_lots(ctx: Ctx, user_id: int, shares: int) -> None:
    """Акции, ушедшие от участника (продажа, подарок, корректировка), сначала
    списываются с акций, купленных на размещении, от старых к новым. Так за
    акции, купленные на рынке, нельзя получить возврат от основателя."""
    lots = (await ctx.s.execute(
        select(Purchase).where(Purchase.user_id == user_id, Purchase.shares_remaining > 0)
        .order_by(Purchase.paid_at, Purchase.id).with_for_update()
    )).scalars().all()
    left = shares
    for lot in lots:
        if left == 0:
            break
        take = min(left, lot.shares_remaining)
        lot.shares_remaining -= take
        left -= take


async def outstanding(ctx: Ctx) -> int:
    """Размещённые акции: все акции вне казны (знаменатель доли голосов)."""
    t = await treasury(ctx)
    total = (await ctx.s.execute(select(func.coalesce(func.sum(Holding.total), 0)))).scalar_one()
    tre = (await holding(ctx, t.id)).total
    return int(total) - tre


async def voting_share(ctx: Ctx, user_id: int) -> float:
    out = await outstanding(ctx)
    if out == 0:
        return 0.0
    return (await holding(ctx, user_id)).total / out


async def _offered_unsold(ctx: Ctx) -> int:
    v = (await ctx.s.execute(
        select(func.coalesce(func.sum(Placement.volume - Placement.sold), 0)).where(Placement.status == "open")
    )).scalar_one()
    return int(v)


async def incoming_exposure(ctx: Ctx, user_id: int) -> int:
    """Акции, которые могут прийти участнику: активные заявки на покупку,
    открытые сделки как покупатель, резервы на размещении, ждущие подарки."""
    q1 = select(func.coalesce(func.sum(Order.shares - Order.filled), 0)).where(
        Order.user_id == user_id, Order.side == "buy", Order.status.in_(("active", "partial")))
    q2 = select(func.coalesce(func.sum(Trade.shares), 0)).where(
        Trade.buyer_id == user_id, Trade.status.in_(TradeStatus.OPEN))
    q3 = select(func.coalesce(func.sum(Purchase.shares), 0)).where(
        Purchase.user_id == user_id, Purchase.status == "reserved")
    q4 = select(func.coalesce(func.sum(Gift.shares), 0)).where(
        Gift.to_user_id == user_id, Gift.status == "pending")
    total = 0
    for q in (q1, q2, q3, q4):
        total += int((await ctx.s.execute(q)).scalar_one())
    return total


async def ownership_base(ctx: Ctx) -> int:
    """База лимита владения: размещённые акции (вне казны) плюс ещё не проданный
    объём открытого размещения. Без второго слагаемого первые покупатели нового
    клуба сразу упирались бы в лимит: при 100 проданных акциях 15% — это 15 штук."""
    return await outstanding(ctx) + await _offered_unsold(ctx)


async def check_ownership_limit(ctx: Ctx, user_id: int, extra: int) -> None:
    """Доля после получения extra акций (с учётом всех ожидающих поступлений:
    заявок на покупку, открытых сделок, резервов, подарков) не должна превысить
    лимит владения."""
    limit_pct = await settings.get(ctx, "ownership_limit_pct")
    h = await holding(ctx, user_id)
    after = h.total + await incoming_exposure(ctx, user_id) + extra
    denom = await ownership_base(ctx)
    if denom <= 0 or after * 100 > limit_pct * denom:
        max_allowed = max(0, (limit_pct * denom) // 100 - (after - extra))
        raise DomainError(
            f"После этого ваша доля превысит лимит владения {limit_pct}%. "
            f"Сейчас можно получить не больше {max_allowed} акций.")


async def check_invariant(ctx: Ctx) -> None:
    """Сумма акций у всех и в казне = выпуску, и баланс = сумма журнала."""
    total = (await ctx.s.execute(select(func.sum(Holding.total)))).scalar_one()
    if int(total) != ctx.config.total_shares:
        raise AssertionError(f"Сумма акций {total} ≠ {ctx.config.total_shares}")
    rows = (await ctx.s.execute(select(Holding))).scalars().all()
    for h in rows:
        inc = (await ctx.s.execute(select(func.coalesce(func.sum(Transfer.shares), 0))
                                   .where(Transfer.to_user_id == h.user_id))).scalar_one()
        out = (await ctx.s.execute(select(func.coalesce(func.sum(Transfer.shares), 0))
                                   .where(Transfer.from_user_id == h.user_id))).scalar_one()
        if int(inc) - int(out) != h.total:
            raise AssertionError(f"Баланс пользователя {h.user_id} не совпадает с журналом")
