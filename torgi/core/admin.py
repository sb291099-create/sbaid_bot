"""Функции администратора: связанные аккаунты, наблюдатели, корректировки,
выгрузки, рассылка. Всё пишется в audit_log."""
from __future__ import annotations

import csv
import io

from sqlalchemy import select

from ..models import AuditLog, Holding, Role, Transfer, User
from ..timeutil import fmt_msk
from . import registry, users
from .audit import audit
from .context import Ctx
from .errors import DomainError


async def set_linked(ctx: Ctx, admin: User, nick: str, linked: bool) -> User:
    users.require_founder(admin)
    u = await users.by_username(ctx, nick)
    if u.role == Role.FOUNDER:
        raise DomainError("Основатель и так не торгует на рынке")
    u.linked_to_founder = linked
    if linked:
        await users.cancel_all_orders(ctx, u.id, reason="аккаунт помечен как связанный с основателем")
    audit(ctx, admin.id, "user.linked" if linked else "user.unlinked", user_id=u.id)
    ctx.publish(f"ℹ️ {u.display} {'помечен как связанный с основателем' if linked else 'больше не связан с основателем'}. "
                f"Связанные аккаунты не торгуют на рынке между участниками.")
    return u


async def linked_list(ctx: Ctx) -> list[User]:
    return list((await ctx.s.execute(select(User).where(User.linked_to_founder.is_(True)))).scalars())


async def set_observer(ctx: Ctx, admin: User, nick: str, on: bool) -> User:
    users.require_founder(admin)
    u = await users.by_username(ctx, nick)
    if u.role == Role.FOUNDER:
        raise DomainError("Нельзя менять роль основателя")
    u.role = Role.OBSERVER if on else (Role.MEMBER if u.full_name else Role.GUEST)
    audit(ctx, admin.id, "user.observer", user_id=u.id, on=on)
    return u


async def adjust(ctx: Ctx, admin: User, nick: str, delta: int, reason: str) -> Transfer:
    """Корректировка баланса — только записью в журнал с причиной; владелец получает уведомление."""
    users.require_founder(admin)
    if not reason or len(reason.strip()) < 3:
        raise DomainError("Укажите причину корректировки")
    if delta == 0:
        raise DomainError("Изменение не может быть нулевым")
    u = await users.by_username(ctx, nick)
    t = await registry.treasury(ctx)
    if delta > 0:
        await registry.check_ownership_limit(ctx, u.id, delta)
        tr = await registry.transfer(ctx, t.id, u.id, delta, "adjustment", f"admin: {reason.strip()}", consume_lots=False)
    else:
        tr = await registry.transfer(ctx, u.id, t.id, -delta, "adjustment", f"admin: {reason.strip()}")
    audit(ctx, admin.id, "balance.adjust", user_id=u.id, delta=delta, reason=reason, transfer_id=tr.id)
    ctx.send(u.id, f"ℹ️ Админ скорректировал ваш баланс: {delta:+d} SBAID. Причина: {reason.strip()}")
    return tr


async def broadcast(ctx: Ctx, admin: User, text: str) -> int:
    users.require_founder(admin)
    ms = await users.members(ctx)
    for m in ms:
        if m.id != admin.id:
            ctx.send(m.id, f"📣 {text}")
    audit(ctx, admin.id, "broadcast", text=text, recipients=len(ms))
    return len(ms)


async def export_registry(ctx: Ctx, actor: User, public: bool) -> str:
    if not public:
        users.require_staff(actor)
    rows = (await ctx.s.execute(select(User, Holding).join(Holding, Holding.user_id == User.id)
                                .where(Holding.total > 0).order_by(Holding.total.desc()))).all()
    out = await registry.outstanding(ctx)
    buf = io.StringIO()
    w = csv.writer(buf)
    if public:
        w.writerow(["ник", "SBAID", "доля голосов, %"])
    else:
        w.writerow(["id", "ник", "ФИО", "контакт", "роль", "связан с основателем", "SBAID", "заблокировано",
                    "доля голосов, %", "оферта", "версия оферты"])
    for u, h in rows:
        share = "" if u.role == Role.TREASURY or out == 0 else f"{h.total * 100 / out:.4f}"
        nick = "казна" if u.role == Role.TREASURY else (u.username or f"id{u.id}")
        if public:
            w.writerow([nick, h.total, share])
        else:
            w.writerow([u.id, nick, u.full_name or "", u.contact or "", u.role, int(u.linked_to_founder),
                        h.total, h.blocked, share, fmt_msk(u.offer_accepted_at), u.offer_version or ""])
    audit(ctx, actor.id, "export.registry", public=public)
    return buf.getvalue()


async def export_journal(ctx: Ctx, actor: User) -> str:
    users.require_staff(actor)
    rows = (await ctx.s.execute(select(Transfer).order_by(Transfer.id))).scalars().all()
    names = {u.id: (u.username or ("казна" if u.role == Role.TREASURY else f"id{u.id}"))
             for u in (await ctx.s.execute(select(User))).scalars()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["№", "время", "от кого", "кому", "SBAID", "тип", "основание"])
    for t in rows:
        w.writerow([t.id, fmt_msk(t.created_at), names.get(t.from_user_id, "выпуск"), names.get(t.to_user_id, ""),
                    t.shares, t.type, t.basis])
    audit(ctx, actor.id, "export.journal")
    return buf.getvalue()


async def audit_tail(ctx: Ctx, actor: User, limit: int = 30) -> list[AuditLog]:
    users.require_staff(actor)
    return list((await ctx.s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))).scalars())
