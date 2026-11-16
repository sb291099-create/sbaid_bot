"""Выход из клуба: погашение акций и возврат денег за акции с размещения.

За акции, купленные на рынке, основатель денег не получал — их можно только
продать или подарить. До 14 дней с покупки — автоматический полный возврат,
позже — по решению админа и не больше уплаченного.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from ..models import Purchase, Refund, User
from ..timeutil import rub
from . import registry, settings, users
from .audit import audit
from .context import Ctx
from .errors import DomainError


async def options(ctx: Ctx, user: User) -> dict:
    days = await settings.get(ctx, "refund_days")
    lots = (await ctx.s.execute(select(Purchase).where(
        Purchase.user_id == user.id, Purchase.shares_remaining > 0).order_by(Purchase.paid_at.desc()))).scalars().all()
    h = await registry.holding(ctx, user.id)
    fresh = sum(l.shares_remaining for l in lots if l.paid_at > ctx.now - timedelta(days=days))
    total = sum(l.shares_remaining for l in lots)
    free = h.total - h.blocked
    return {"redeemable": min(total, free), "auto_refund": min(fresh, free), "free": free, "refund_days": days}


async def request(ctx: Ctx, user: User, shares: int) -> list[Refund]:
    users.require_member(user)
    if shares <= 0:
        raise DomainError("Количество акций должно быть положительным")
    opts = await options(ctx, user)
    if shares > opts["redeemable"]:
        raise DomainError(f"Погасить с возвратом можно не больше {opts['redeemable']} акций — "
                          f"это свободные акции, купленные на размещении. Акции с рынка можно только "
                          f"продать (/sell) или подарить (/gift).")
    days = opts["refund_days"]
    t = await registry.treasury(ctx)
    lots = (await ctx.s.execute(select(Purchase).where(
        Purchase.user_id == user.id, Purchase.shares_remaining > 0)
        .order_by(Purchase.paid_at.desc(), Purchase.id.desc()).with_for_update())).scalars().all()
    left, out = shares, []
    for lot in lots:  # от новых к старым: свежие покупки попадают в возврат без вопросов
        if left == 0:
            break
        take = min(left, lot.shares_remaining)
        left -= take
        lot.shares_remaining -= take
        paid = take * lot.amount // lot.shares
        auto = lot.paid_at > ctx.now - timedelta(days=days)
        r = Refund(user_id=user.id, purchase_id=lot.id, shares=take, max_amount=paid,
                   amount=paid if auto else None, reason="выход из клуба", kind="auto" if auto else "admin",
                   status="processing" if auto else "pending_admin")
        ctx.s.add(r)
        await ctx.s.flush()
        if auto:
            await registry.transfer(ctx, user.id, t.id, take, "exit", f"refund:{r.id}", consume_lots=False)
        else:
            await registry.block(ctx, user.id, take)
        audit(ctx, user.id, "refund.request", refund_id=r.id, shares=take, kind=r.kind, max_amount=paid)
        out.append(r)
    auto_sum = sum(r.amount for r in out if r.kind == "auto")
    admin_n = sum(r.shares for r in out if r.kind == "admin")
    msg = []
    if auto_sum:
        msg.append(f"Акции погашены, возврат {rub(auto_sum)} оформляется через Robokassa с чеком возврата.")
    if admin_n:
        msg.append(f"{admin_n} акций куплены больше {days} дней назад — заявка на возврат ушла основателю, "
                   f"акции заблокированы до решения.")
        ctx.staff(f"🚪 Заявка на выход: {user.display} ({user.full_name}), {admin_n} акций. /refunds")
    ctx.send(user.id, "\n".join(msg))
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
        ctx.send(r.user_id, f"Заявка на возврат за {r.shares} акций отклонена: {comment}. Акции разблокированы.")
        return r
    if amount < 0 or amount > r.max_amount:
        raise DomainError(f"Сумма возврата не может быть больше уплаченной: {rub(r.max_amount)}")
    t = await registry.treasury(ctx)
    await registry.transfer(ctx, r.user_id, t.id, r.shares, "exit", f"refund:{r.id}", from_blocked=True,
                            consume_lots=False)
    r.amount = amount
    r.status = "processing" if amount > 0 else "done"
    audit(ctx, admin.id, "refund.approve", refund_id=r.id, amount=amount, comment=comment)
    ctx.send(r.user_id, f"Основатель одобрил выход: {r.shares} акций погашены"
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
