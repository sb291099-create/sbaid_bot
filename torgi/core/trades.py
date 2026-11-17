"""Сделка на бирже: оплата, подтверждение, спор и сроки.

Бот никогда не переносит акции, пока продавец не подтвердил получение денег
или админ не решил спор в пользу переноса.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select

from ..models import Dispute, Trade, TradeStatus as TS, User
from ..timeutil import fmt_msk, rub
from . import registry, settings, users
from .audit import audit
from .context import Button, Ctx
from .errors import DomainError


async def get_locked(ctx: Ctx, trade_id: int) -> Trade:
    t = (await ctx.s.execute(select(Trade).where(Trade.id == trade_id).with_for_update())).scalar_one_or_none()
    if t is None:
        raise DomainError("Сделка не найдена")
    return t


def _set_status(ctx: Ctx, t: Trade, status: str, actor_id: int | None, **extra) -> None:
    audit(ctx, actor_id, "trade.status", trade_id=t.id, old=t.status, new=status, **extra)
    t.status = status


async def seller_requisites(ctx: Ctx, viewer: User, trade_id: int) -> str:
    """Реквизиты продавца видит только покупатель из его пары и только до завершения сделки."""
    t = await ctx.s.get(Trade, trade_id)
    if t is None or t.buyer_id != viewer.id or t.status not in TS.OPEN:
        raise DomainError("Реквизиты недоступны")
    seller = await users.get(ctx, t.seller_id)
    if not seller.payout_details_enc:
        raise DomainError("Продавец не указал реквизиты — напишите админу")
    return ctx.cipher.decrypt(seller.payout_details_enc)


def buyer_card(t: Trade, requisites: str) -> tuple[str, list]:
    text = (f"🤝 Сделка №{t.id}: вы покупаете {t.shares} SBAID по {rub(t.price)}.\n"
            f"Переведите продавцу {rub(t.amount)} до {fmt_msk(t.pay_deadline)}.\n"
            f"Реквизиты: {requisites}\n\n"
            f"После перевода нажмите «Я оплатил» и при желании пришлите скриншот перевода.")
    kb = [[Button("Я оплатил", f"tr:paid:{t.id}")], [Button("Прислать доказательство", f"tr:proof:{t.id}")]]
    return text, kb


def seller_card(t: Trade) -> str:
    return (f"🤝 Сделка №{t.id}: вы продаёте {t.shares} SBAID по {rub(t.price)} на сумму {rub(t.amount)}.\n"
            f"Покупатель должен перевести деньги до {fmt_msk(t.pay_deadline)}. SBAID заблокированы до "
            f"завершения сделки. Как только деньги придут, подтвердите получение.")


async def buyer_paid(ctx: Ctx, user: User, trade_id: int) -> Trade:
    t = await get_locked(ctx, trade_id)
    if t.buyer_id != user.id:
        raise DomainError("Это не ваша сделка")
    if t.status != TS.AWAITING_PAYMENT:
        raise DomainError("Отметка об оплате уже не нужна")
    if ctx.now > t.pay_deadline:
        raise DomainError("Срок оплаты прошёл")
    t.buyer_paid_at = ctx.now
    _set_status(ctx, t, TS.AWAITING_CONFIRMATION, user.id)
    ctx.send(t.seller_id,
             f"💸 Покупатель по сделке №{t.id} отметил, что перевёл {rub(t.amount)}. Проверьте поступление "
             f"и подтвердите до {fmt_msk(t.confirm_deadline)}.",
             [[Button("Деньги получил", f"tr:confirm:{t.id}")], [Button("Денег нет", f"tr:nomoney:{t.id}")]])
    return t


async def seller_confirm(ctx: Ctx, user: User, trade_id: int) -> Trade:
    t = await get_locked(ctx, trade_id)
    if t.seller_id != user.id:
        raise DomainError("Это не ваша сделка")
    if t.status not in (TS.AWAITING_PAYMENT, TS.AWAITING_CONFIRMATION):
        raise DomainError("Сделку уже нельзя подтвердить")
    t.seller_confirmed_at = ctx.now
    await _complete(ctx, t, user.id, basis_note="подтверждение продавца")
    return t


async def _complete(ctx: Ctx, t: Trade, actor_id: int | None, basis_note: str) -> None:
    await registry.transfer(ctx, t.seller_id, t.buyer_id, t.shares, "trade", f"trade:{t.id}", from_blocked=True)
    t.completed_at = ctx.now
    _set_status(ctx, t, TS.COMPLETED, actor_id, reason=basis_note)
    buyer, seller = await users.get(ctx, t.buyer_id), await users.get(ctx, t.seller_id)
    buyer.completed_trades += 1
    seller.completed_trades += 1
    deed = (f"📄 Уступка по сделке №{t.id}\n"
            f"Продавец: {seller.full_name} ({seller.display})\n"
            f"Покупатель: {buyer.full_name} ({buyer.display})\n"
            f"SBAID: {t.shares}, цена {rub(t.price)}, сумма {rub(t.amount)}\n"
            f"Дата: {fmt_msk(ctx.now)}\nОснование: {basis_note}.")
    ctx.send(t.buyer_id, deed)
    ctx.send(t.seller_id, deed)
    if not buyer.group_invite_sent:
        buyer.group_invite_sent = True
        ctx.invite(buyer.id)


async def _cancel(ctx: Ctx, t: Trade, actor_id: int | None, reason: str) -> None:
    await registry.unblock(ctx, t.seller_id, t.shares)
    _set_status(ctx, t, TS.CANCELLED, actor_id, reason=reason)
    for uid in (t.buyer_id, t.seller_id):
        ctx.send(uid, f"Сделка №{t.id} отменена: {reason}. SBAID продавца разблокированы.")


async def open_dispute(ctx: Ctx, t: Trade, opened_by: int | None, reason: str) -> Dispute:
    days = await settings.get(ctx, "dispute_days")
    d = Dispute(trade_id=t.id, opened_by=opened_by, reason=reason, evidence=list(t.evidence or []),
                deadline=ctx.now + timedelta(days=days), created_at=ctx.now)
    ctx.s.add(d)
    _set_status(ctx, t, TS.DISPUTED, opened_by, reason=reason)
    await ctx.s.flush()
    for uid in (t.buyer_id, t.seller_id):
        hint = "скриншот или квитанцию перевода" if uid == t.buyer_id else "выписку из банка за день"
        ctx.send(uid, f"⚖️ По сделке №{t.id} открыт спор: {reason}.\nПришлите {hint}. Админ решит до "
                      f"{fmt_msk(d.deadline)}; SBAID продавца до решения заблокированы.",
                 [[Button("Прислать доказательство", f"tr:proof:{t.id}")]])
    ctx.staff(f"⚖️ Новый спор №{d.id} по сделке №{t.id} ({t.shares} SBAID, {rub(t.amount)}): {reason}.\n"
              f"Решить до {fmt_msk(d.deadline)}. /disputes")
    return d


async def seller_no_money(ctx: Ctx, user: User, trade_id: int) -> Dispute:
    t = await get_locked(ctx, trade_id)
    if t.seller_id != user.id:
        raise DomainError("Это не ваша сделка")
    if t.status not in (TS.AWAITING_PAYMENT, TS.AWAITING_CONFIRMATION):
        raise DomainError("По этой сделке нельзя открыть спор")
    return await open_dispute(ctx, t, user.id, "продавец сообщил, что денег нет")


async def add_evidence(ctx: Ctx, user: User, trade_id: int, file_id: str, kind: str) -> None:
    t = await get_locked(ctx, trade_id)
    if user.id not in (t.buyer_id, t.seller_id):
        raise DomainError("Это не ваша сделка")
    if t.status not in TS.OPEN:
        raise DomainError("Сделка уже закрыта")
    item = {"user_id": user.id, "file_id": file_id, "kind": kind, "at": ctx.now.isoformat()}
    t.evidence = [*(t.evidence or []), item]
    d = (await ctx.s.execute(select(Dispute).where(Dispute.trade_id == t.id).with_for_update())).scalar_one_or_none()
    if d is not None:
        d.evidence = [*(d.evidence or []), item]
    audit(ctx, user.id, "trade.evidence", trade_id=t.id, dispute_id=d.id if d else None, kind=kind)


async def resolve_dispute(ctx: Ctx, admin: User, dispute_id: int, decision: str, comment: str) -> Dispute:
    users.require_founder(admin)
    if decision not in ("transfer", "cancel"):
        raise DomainError("Решение — «перенести» или «отменить»")
    if not comment or len(comment.strip()) < 3:
        raise DomainError("Комментарий к решению обязателен")
    d = (await ctx.s.execute(select(Dispute).where(Dispute.id == dispute_id).with_for_update())).scalar_one_or_none()
    if d is None or d.decision:
        raise DomainError("Спор не найден или уже решён")
    t = await get_locked(ctx, d.trade_id)
    d.decision, d.comment, d.decided_by, d.decided_at = decision, comment.strip(), admin.id, ctx.now
    audit(ctx, admin.id, "dispute.resolve", dispute_id=d.id, trade_id=t.id, decision=decision,
          comment=comment)
    if decision == "transfer":
        await _complete(ctx, t, admin.id, basis_note=f"решение админа по спору №{d.id}")
    else:
        await _cancel(ctx, t, admin.id, f"решение админа по спору №{d.id}: {comment.strip()}")
    for uid in (t.buyer_id, t.seller_id):
        ctx.send(uid, f"⚖️ Спор №{d.id} решён: {'SBAID переносятся' if decision == 'transfer' else 'сделка отменена'}."
                      f"\nКомментарий админа: {comment.strip()}")
    return d


async def process_deadlines(ctx: Ctx) -> None:
    """Истёк срок оплаты — сделка отменяется, SBAID продавца разблокируются.
    Истёк срок подтверждения — автоматически открывается спор (акции без
    подтверждения продавца не переносятся)."""
    overdue_pay = (await ctx.s.execute(select(Trade).where(
        Trade.status == TS.AWAITING_PAYMENT, Trade.pay_deadline <= ctx.now).with_for_update())).scalars().all()
    for t in overdue_pay:
        await _cancel(ctx, t, None, "покупатель не оплатил вовремя")
    overdue_conf = (await ctx.s.execute(select(Trade).where(
        Trade.status == TS.AWAITING_CONFIRMATION, Trade.confirm_deadline <= ctx.now).with_for_update())).scalars().all()
    for t in overdue_conf:
        await open_dispute(ctx, t, None, "продавец не подтвердил получение денег в срок")
    late = (await ctx.s.execute(select(Dispute).where(
        Dispute.decision.is_(None), Dispute.deadline <= ctx.now))).scalars().all()
    for d in late:
        key = f"dispute_overdue:{d.id}"
        t = await ctx.s.get(Trade, d.trade_id)
        if key not in (t.reminders_sent or []):
            t.reminders_sent = [*(t.reminders_sent or []), key]
            ctx.staff(f"⏰ Спор №{d.id} не решён в срок. /disputes")


async def send_reminders(ctx: Ctx) -> None:
    hours = await settings.get(ctx, "reminder_hours")
    soon = ctx.now + timedelta(hours=hours)
    rows = (await ctx.s.execute(select(Trade).where(
        or_(Trade.status == TS.AWAITING_PAYMENT, Trade.status == TS.AWAITING_CONFIRMATION)).with_for_update())).scalars().all()
    for t in rows:
        sent = list(t.reminders_sent or [])
        if t.status == TS.AWAITING_PAYMENT and t.pay_deadline <= soon and "pay" not in sent:
            ctx.send(t.buyer_id, f"⏰ Сделка №{t.id}: осталось меньше {hours} ч на оплату {rub(t.amount)} "
                                 f"(до {fmt_msk(t.pay_deadline)}).", [[Button("Я оплатил", f"tr:paid:{t.id}")]])
            sent.append("pay")
        if t.status == TS.AWAITING_CONFIRMATION and t.confirm_deadline <= soon and "confirm" not in sent:
            ctx.send(t.seller_id, f"⏰ Сделка №{t.id}: подтвердите получение {rub(t.amount)} до "
                                  f"{fmt_msk(t.confirm_deadline)}.",
                     [[Button("Деньги получил", f"tr:confirm:{t.id}")], [Button("Денег нет", f"tr:nomoney:{t.id}")]])
            sent.append("confirm")
        if sent != (t.reminders_sent or []):
            t.reminders_sent = sent


async def user_trades(ctx: Ctx, user_id: int, limit: int = 20) -> list[Trade]:
    return list((await ctx.s.execute(select(Trade).where(
        or_(Trade.buyer_id == user_id, Trade.seller_id == user_id)).order_by(Trade.id.desc()).limit(limit))).scalars())


async def open_disputes(ctx: Ctx) -> list[Dispute]:
    return list((await ctx.s.execute(select(Dispute).where(Dispute.decision.is_(None)).order_by(Dispute.id))).scalars())

