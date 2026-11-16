"""Выход из клуба и возврат денег за SBAID с размещения.

Основной выход — продажа на рынке по рыночной цене; основатель SBAID не выкупает.
По закону участник может отказаться от участия и вернуть SBAID, купленные на
размещении: по заявлению, через основателя, не больше уплаченного за эти SBAID
(за вычетом фактических расходов) и в течение 10 дней. За SBAID, купленные на
рынке или полученные в подарок, основатель денег не получал — их только продают
или дарят.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from ..models import Purchase, Refund, User
from ..timeutil import fmt_msk, rub
from . import registry, users
from .audit import audit
from .context import Ctx
from .errors import DomainError

ANSWER_DAYS = 10  # срок ответа на заявление о возврате по закону о защите прав потребителей


async def options(ctx: Ctx, user: User) -> dict:
    lots = (await ctx.s.execute(select(Purchase).where(
        Purchase.user_id == user.id, Purchase.shares_remaining > 0))).scalars().all()
    h = await registry.holding(ctx, user.id)
    total = sum(l.shares_remaining for l in lots)
    free = h.total - h.blocked
    return {"redeemable": min(total, free), "free": free}


async def request(ctx: Ctx, user: User, shares: int) -> list[Refund]:
    """Заявление о возврате: SBAID блокируются, решение и сумму определяет основатель."""
    users.require_member(user)
    if shares <= 0:
        raise DomainError("Количество SBAID должно быть положительным")
    opts = await options(ctx, user)
    if shares > opts["redeemable"]:
        raise DomainError(f"Вернуть основателю можно не больше {opts['redeemable']} SBAID — "
                          f"это свободные SBAID, купленные на размещении. SBAID с рынка можно только "
                          f"продать (/sell) или подарить (/gift).")
    lots = (await ctx.s.execute(select(Purchase).where(
        Purchase.user_id == user.id, Purchase.shares_remaining > 0)
        .order_by(Purchase.paid_at.desc(), Purchase.id.desc()).with_for_update())).scalars().all()
    left, out = shares, []
    for lot in lots:  # от новых покупок к старым
        if left == 0:
            break
        take = min(left, lot.shares_remaining)
        left -= take
        lot.shares_remaining -= take
        paid = take * lot.amount // lot.shares
        r = Refund(user_id=user.id, purchase_id=lot.id, shares=take, max_amount=paid, amount=None,
                   reason="заявление о выходе", kind="admin", status="pending_admin")
        ctx.s.add(r)
        await ctx.s.flush()
        await registry.block(ctx, user.id, take)
        audit(ctx, user.id, "refund.request", refund_id=r.id, shares=take, max_amount=paid)
        out.append(r)
    paid_total = sum(r.max_amount for r in out)
    due = ctx.now + timedelta(days=ANSWER_DAYS)
    ctx.send(user.id, f"Заявление принято: {shares} SBAID заблокированы до решения основателя. "
                      f"Уплачено за них {rub(paid_total)}; возврат — не больше этой суммы за вычетом "
                      f"фактических расходов. Ответ — до {fmt_msk(due)}.")
    ctx.staff(f"🚪 Заявление о возврате: {user.display} ({user.full_name}), {shares} SBAID, уплачено "
              f"{rub(paid_total)}. Ответить до {fmt_msk(due)}. /refunds")
    return out


async def decide(ctx: Ctx, admin: User, refund_id: int, amount: int | None, comment: str) -> Refund:
    """amount=None — отказ (акции разблокируются); 0 — погасить без возврата денег."""
    users.require_founder(admin)
    r = (await ctx.s.execute(select(Refund).where(Refund.id == refund_id).with_for_update())).scalar_one_or_none()
    if r is None or r.status != "pending_admin":
        raise DomainError("Заявка не найдена или уже обработана")
    if amount is None:
        r.status = "rejected"
        await registry.unblock(ctx, r.user_id, r.shares)
        lot = (await ctx.s.execute(select(Purchase).where(Purchase.id == r.purchase_id).with_for_update())).scalar_one()
        lot.shares_remaining += r.shares
        audit(ctx, admin.id, "refund.reject", refund_id=r.id, comment=comment)
        ctx.send(r.user_id, f"Заявка на возврат за {r.shares} SBAID отклонена: {comment}. SBAID разблокированы.")
        return r
    if amount < 0 or amount > r.max_amount:
        raise DomainError(f"Сумма возврата не может быть больше уплаченной: {rub(r.max_amount)}")
    t = await registry.treasury(ctx)
    await registry.transfer(ctx, r.user_id, t.id, r.shares, "exit", f"refund:{r.id}", from_blocked=True,
                            consume_lots=False)
    r.amount = amount
    r.status = "processing" if amount > 0 else "done"
    audit(ctx, admin.id, "refund.approve", refund_id=r.id, amount=amount, comment=comment)
    ctx.send(r.user_id, f"Основатель одобрил выход: {r.shares} SBAID погашены"
                        + (f", возврат {rub(amount)} оформляется." if amount else " без возврата денег.")
                        + (f"\nКомментарий: {comment}" if comment else ""))
    return r


async def pending(ctx: Ctx) -> list[Refund]:
    return list((await ctx.s.execute(select(Refund).where(Refund.status.in_(("pending_admin", "processing", "failed")))
                                     .order_by(Refund.id))).scalars())


async def mark_done(ctx: Ctx, admin: User, refund_id: int, note: str) -> Refund:
    """Отметить возврат, проведённый вручную в личном кабинете Robokassa."""
    users.require_founder(admin)
    r = (await ctx.s.execute(select(Refund).where(Refund.id == refund_id).with_for_update())).scalar_one_or_none()
    if r is None or r.status not in ("processing", "failed"):
        raise DomainError("Возврат не найден или не ждёт проведения")
    r.status, r.robokassa_refund_id = "done", note or "manual"
    audit(ctx, admin.id, "refund.manual_done", refund_id=r.id, note=note)
    ctx.send(r.user_id, f"Возврат {rub(r.amount)} проведён.")
    return r


async def process_queue(app) -> None:
    """Провести возвраты в статусе processing через Robokassa (вне транзакции БД)."""
    rk = app.payments
    async with app.tx() as ctx:
        ids = (await ctx.s.execute(select(Refund.id).where(
            Refund.status == "processing", Refund.robokassa_refund_id.is_(None)))).scalars().all()
    for rid in ids:
        async with app.tx() as ctx:
            r = (await ctx.s.execute(select(Refund).where(Refund.id == rid).with_for_update(skip_locked=True))).scalar_one_or_none()
            if r is None or r.status != "processing" or r.robokassa_refund_id:
                continue
            if rk is None or not rk.configured or not rk.p3:
                r.status = "failed"
                audit(ctx, None, "refund.no_api", refund_id=r.id)
                ctx.staff(f"💳 Возврат №{r.id} на {rub(r.amount)} нужно провести вручную в кабинете Robokassa "
                          f"(счёт №{r.purchase_id}), затем /refund_done {r.id}")
                continue
            r.robokassa_refund_id = "pending"  # защита от повторной отправки
        try:
            ref_id = await rk.refund(r.purchase_id, r.amount, ctx.config.receipt_item_name.format(n=r.shares))
            new_status, ref = "done", ref_id
        except Exception as e:  # noqa: BLE001
            new_status, ref = "failed", None
            err = str(e)
        async with app.tx() as ctx:
            r = (await ctx.s.execute(select(Refund).where(Refund.id == rid).with_for_update())).scalar_one()
            r.status, r.robokassa_refund_id = new_status, ref
            audit(ctx, None, f"refund.{new_status}", refund_id=r.id)
            if new_status == "done":
                ctx.send(r.user_id, f"💳 Возврат {rub(r.amount)} отправлен, чек возврата придёт от Robokassa.")
            else:
                ctx.staff(f"❗ Возврат №{r.id} не прошёл через Robokassa: {err}. Проведите вручную и /refund_done {r.id}")
