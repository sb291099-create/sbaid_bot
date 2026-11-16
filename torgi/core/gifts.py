"""Подарок акций другому участнику (получатель подтверждает)."""
from __future__ import annotations

from sqlalchemy import select

from ..models import Gift, User
from . import registry, users
from .audit import audit
from .context import Button, Ctx
from .errors import DomainError


async def offer(ctx: Ctx, giver: User, recipient_nick: str, shares: int) -> Gift:
    users.require_member(giver)
    to = await users.by_username(ctx, recipient_nick)
    if to.id == giver.id:
        raise DomainError("Нельзя подарить акции самому себе")
    if not users.is_registered(to):
        raise DomainError("Получатель ещё не зарегистрирован в клубе")
    if shares <= 0:
        raise DomainError("Количество акций должно быть положительным")
    await registry.check_ownership_limit(ctx, to.id, shares)
    await registry.block(ctx, giver.id, shares)
    g = Gift(from_user_id=giver.id, to_user_id=to.id, shares=shares, status="pending")
    ctx.s.add(g)
    await ctx.s.flush()
    audit(ctx, giver.id, "gift.offer", gift_id=g.id, to=to.id, shares=shares)
    ctx.send(to.id, f"🎁 {giver.display} дарит вам {shares} акций клуба. Принять?",
             [[Button("Принять", f"gift:accept:{g.id}"), Button("Отказаться", f"gift:decline:{g.id}")]])
    return g


async def _locked(ctx: Ctx, gift_id: int) -> Gift:
    g = (await ctx.s.execute(select(Gift).where(Gift.id == gift_id).with_for_update())).scalar_one_or_none()
    if g is None or g.status != "pending":
        raise DomainError("Подарок не найден или уже обработан")
    return g


async def accept(ctx: Ctx, recipient: User, gift_id: int) -> Gift:
    g = await _locked(ctx, gift_id)
    if g.to_user_id != recipient.id:
        raise DomainError("Этот подарок не вам")
    # этот подарок уже учтён во «входящих», поэтому extra=0
    try:
        await registry.check_ownership_limit(ctx, recipient.id, 0)
    except DomainError:
        g.status = "rejected_limit"
        await registry.unblock(ctx, g.from_user_id, g.shares)
        audit(ctx, recipient.id, "gift.rejected_limit", gift_id=g.id)
        ctx.send(g.from_user_id, f"Подарок {g.shares} акций не принят: у получателя превышен бы лимит владения.")
        raise
    await registry.transfer(ctx, g.from_user_id, g.to_user_id, g.shares, "gift", f"gift:{g.id}", from_blocked=True)
    g.status = "accepted"
    audit(ctx, recipient.id, "gift.accept", gift_id=g.id)
    ctx.send(g.from_user_id, f"🎁 {recipient.display} принял подарок: {g.shares} акций.")
    if not recipient.group_invite_sent:
        recipient.group_invite_sent = True
        ctx.invite(recipient.id)
    return g


async def decline(ctx: Ctx, user: User, gift_id: int) -> Gift:
    g = await _locked(ctx, gift_id)
    if user.id not in (g.to_user_id, g.from_user_id):
        raise DomainError("Это не ваш подарок")
    await registry.unblock(ctx, g.from_user_id, g.shares)
    g.status = "declined" if user.id == g.to_user_id else "cancelled"
    audit(ctx, user.id, f"gift.{g.status}", gift_id=g.id)
    other = g.from_user_id if user.id == g.to_user_id else g.to_user_id
    ctx.send(other, f"Подарок {g.shares} акций {'отклонён' if g.status == 'declined' else 'отменён'}.")
    return g
