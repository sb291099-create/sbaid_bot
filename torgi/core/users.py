"""Участники: регистрация, роли, реквизиты, штрафные баллы."""
from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select

from ..models import Order, Penalty, Role, User
from . import registry, settings
from .audit import audit
from .context import Ctx
from .errors import DomainError

STAFF_ROLES = (Role.FOUNDER, Role.OBSERVER)


async def get(ctx: Ctx, user_id: int) -> User:
    u = await ctx.s.get(User, user_id)
    if u is None:
        raise DomainError("Пользователь не найден")
    return u


async def get_or_create_tg(ctx: Ctx, telegram_id: int, username: str | None) -> User:
    u = (await ctx.s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if u is None:
        role = Role.FOUNDER if telegram_id == ctx.config.founder_telegram_id else Role.GUEST
        u = User(telegram_id=telegram_id, username=username, role=role)
        ctx.s.add(u)
        await ctx.s.flush()
        await registry.holding(ctx, u.id)
        audit(ctx, u.id, "user.start", telegram_id=telegram_id, role=role)
    else:
        if username and u.username != username:
            u.username = username
        if telegram_id == ctx.config.founder_telegram_id and u.role != Role.FOUNDER:
            u.role = Role.FOUNDER
    return u


async def by_username(ctx: Ctx, nick: str) -> User:
    nick = nick.strip().lstrip("@")
    u = (await ctx.s.execute(select(User).where(func.lower(User.username) == nick.lower()))).scalar_one_or_none()
    if u is None or u.role == Role.TREASURY:
        raise DomainError(f"Участник @{nick} не найден. Он должен хотя бы раз нажать /start в боте.")
    return u


async def accept_offer(ctx: Ctx, user_id: int) -> None:
    u = await get(ctx, user_id)
    u.offer_accepted_at = ctx.now
    u.offer_version = ctx.config.offer_version
    audit(ctx, u.id, "user.offer_accepted", version=u.offer_version)


async def consent_pd(ctx: Ctx, user_id: int) -> None:
    u = await get(ctx, user_id)
    u.pd_consent_at = ctx.now
    audit(ctx, u.id, "user.pd_consent")


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^\+?[0-9\-\s()]{10,18}$")


async def set_profile(ctx: Ctx, user_id: int, full_name: str, contact: str) -> User:
    u = await get(ctx, user_id)
    if not u.offer_accepted_at or not u.pd_consent_at:
        raise DomainError("Сначала примите оферту и дайте согласие на обработку персональных данных")
    full_name = " ".join(full_name.split())
    if len(full_name.split()) < 2:
        raise DomainError("Укажите ФИО полностью — оно нужно для договора уступки")
    contact = contact.strip()
    if not (EMAIL.match(contact) or PHONE.match(contact)):
        raise DomainError("Укажите телефон или e-mail")
    u.full_name, u.contact = full_name, contact
    if u.role == Role.GUEST:
        u.role = Role.MEMBER
        audit(ctx, u.id, "user.registered")
    return u


def is_registered(u: User) -> bool:
    return u.role in (Role.MEMBER, Role.FOUNDER, Role.OBSERVER) and bool(u.full_name)


def require_member(u: User) -> None:
    if not is_registered(u):
        raise DomainError("Сначала зарегистрируйтесь: /start")


def founder_side(u: User) -> bool:
    return u.role == Role.FOUNDER or u.linked_to_founder


def require_founder(u: User) -> None:
    if u.role != Role.FOUNDER:
        raise DomainError("Команда доступна только основателю")


def require_staff(u: User) -> None:
    if u.role not in STAFF_ROLES:
        raise DomainError("Команда доступна только основателю и совету директоров")


async def set_payout_details(ctx: Ctx, user_id: int, phone: str, bank: str) -> None:
    if not PHONE.match(phone.strip()):
        raise DomainError("Укажите телефон для перевода по СБП")
    if len(bank.strip()) < 2:
        raise DomainError("Укажите банк")
    u = await get(ctx, user_id)
    u.payout_details_enc = ctx.cipher.encrypt(f"СБП: {phone.strip()}, банк: {bank.strip()}")
    audit(ctx, u.id, "user.payout_details_set")


async def penalty_points(ctx: Ctx, user_id: int) -> int:
    window = await settings.get(ctx, "penalty_window_days")
    v = (await ctx.s.execute(select(func.coalesce(func.sum(Penalty.points), 0)).where(
        Penalty.user_id == user_id, Penalty.created_at > ctx.now - timedelta(days=window)))).scalar_one()
    return int(v)


async def add_penalty(ctx: Ctx, user_id: int, points: int, reason: str, trade_id: int | None = None,
                      actor_id: int | None = None) -> None:
    ctx.s.add(Penalty(user_id=user_id, points=points, reason=reason, trade_id=trade_id, created_at=ctx.now))
    await ctx.s.flush()
    audit(ctx, actor_id, "penalty.add", user_id=user_id, points=points, reason=reason, trade_id=trade_id)
    total = await penalty_points(ctx, user_id)
    limit, ban_days = await settings.get_many(ctx, "penalty_ban_points", "ban_days")
    msg = f"⚠️ Вам начислено штрафных баллов: {points} ({reason}). За последние полгода: {total}."
    if total >= limit:
        u = await get(ctx, user_id)
        u.banned_until = ctx.now + timedelta(days=ban_days)
        await cancel_all_orders(ctx, user_id, reason="запрет на торговлю")
        audit(ctx, None, "user.banned", user_id=user_id, until=u.banned_until)
        from ..timeutil import fmt_msk
        msg += f"\nТорговля запрещена до {fmt_msk(u.banned_until)}, активные заявки отменены."
    ctx.send(user_id, msg)


async def cancel_all_orders(ctx: Ctx, user_id: int, reason: str) -> None:
    orders = (await ctx.s.execute(select(Order).where(
        Order.user_id == user_id, Order.status.in_(("active", "partial"))).with_for_update())).scalars().all()
    for o in orders:
        if o.side == "sell":
            await registry.unblock(ctx, user_id, o.remaining)
        o.status = "cancelled"
        audit(ctx, None, "order.cancelled", order_id=o.id, reason=reason)


async def members(ctx: Ctx) -> list[User]:
    return list((await ctx.s.execute(select(User).where(
        User.role.in_((Role.MEMBER, Role.FOUNDER, Role.OBSERVER)), User.telegram_id.is_not(None)))).scalars())


async def staff(ctx: Ctx) -> list[User]:
    return list((await ctx.s.execute(select(User).where(User.role.in_(STAFF_ROLES)))).scalars())


async def founder(ctx: Ctx) -> User | None:
    return (await ctx.s.execute(select(User).where(User.role == Role.FOUNDER).limit(1))).scalar_one_or_none()

