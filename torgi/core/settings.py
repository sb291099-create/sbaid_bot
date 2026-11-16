"""Настройки клуба. Все числа из раздела «Настройки и лимиты» ТЗ — здесь,
а не константами в коде. Изменение пишется в журнал, публикуется в канале
и вступает в силу не раньше чем через 7 дней."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from ..models import SettingChange
from .audit import audit
from .context import Ctx
from .errors import DomainError

# ключ: (значение по умолчанию, описание для админа и канала)
DEFAULTS: dict[str, tuple[Any, str]] = {
    "placement_lot": (5, "Лот на размещении, акций"),
    "placement_user_limit_pct": (10, "Лимит в одни руки на размещении, % объёма партии"),
    "ownership_limit_pct": (15, "Лимит владения, % всех размещённых акций"),
    "reserve_minutes": (30, "Резерв акций на время оплаты, минут"),
    "cutoff_weekday": (6, "День отсечки торгового дня (0 — пн … 6 — вс)"),
    "cutoff_hour": (12, "Час отсечки торгового дня, МСК"),
    "pay_weekday": (0, "День срока оплаты покупателем (0 — пн)"),
    "pay_hour": (12, "Час срока оплаты, МСК"),
    "confirm_weekday": (1, "День срока подтверждения продавцом (1 — вт)"),
    "confirm_hour": (12, "Час срока подтверждения, МСК"),
    "dispute_days": (3, "Срок решения спора, дней"),
    "penalty_ban_points": (3, "Штрафных баллов до запрета на торговлю"),
    "penalty_window_days": (180, "Окно подсчёта штрафных баллов, дней"),
    "ban_days": (90, "Срок запрета на торговлю, дней"),
    "order_price_band_pct": (100, "Коридор цены в заявке, ± % от референсной"),
    "order_max_weeks": (4, "Максимальный срок жизни заявки, недель"),
    "reminder_hours": (6, "Напоминание до срока, часов"),
    "price_window_days": (30, "Окно расчёта цены размещения, дней"),
    "price_min_trades": (10, "Минимум сделок для пересчёта цены размещения"),
    "price_min_buyers": (5, "Минимум разных покупателей для пересчёта цены"),
    "price_corridor_down_pct": (30, "Коридор цены размещения вниз, %"),
    "price_corridor_up_pct": (50, "Коридор цены размещения вверх, %"),
    "price_floor_kop": (10_000, "Нижняя граница цены размещения, коп."),
    "price_step_kop": (500, "Шаг округления цены размещения, коп."),
    "refund_days": (14, "Возврат без вопросов, дней с покупки"),
    "initial_reference_price_kop": (10_000, "Референсная цена до первого размещения, коп."),
}

EFFECTIVE_DELAY = timedelta(days=7)


async def get(ctx: Ctx, key: str) -> Any:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = (await ctx.s.execute(
        select(SettingChange)
        .where(SettingChange.key == key, SettingChange.effective_at <= ctx.now)
        .order_by(SettingChange.effective_at.desc(), SettingChange.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    return row.value["v"] if row else DEFAULTS[key][0]


async def get_many(ctx: Ctx, *keys: str) -> list[Any]:
    return [await get(ctx, k) for k in keys]


async def change(ctx: Ctx, actor_id: int, key: str, value: int) -> SettingChange:
    if key not in DEFAULTS:
        raise DomainError(f"Нет такой настройки: {key}")
    if not isinstance(value, int) or value < 0:
        raise DomainError("Значение должно быть целым неотрицательным числом")
    current = await get(ctx, key)
    sc = SettingChange(key=key, value={"v": value}, published_at=ctx.now,
                       effective_at=ctx.now + EFFECTIVE_DELAY, changed_by=actor_id)
    ctx.s.add(sc)
    audit(ctx, actor_id, "setting.change", key=key, old=current, new=value,
          effective_at=sc.effective_at)
    from ..timeutil import fmt_msk
    ctx.publish(f"⚙️ Изменение правил клуба\n{DEFAULTS[key][1]}: {current} → {value}\n"
                f"Вступает в силу {fmt_msk(sc.effective_at)}.")
    return sc


async def pending_changes(ctx: Ctx) -> list[SettingChange]:
    return list((await ctx.s.execute(
        select(SettingChange).where(SettingChange.effective_at > ctx.now).order_by(SettingChange.effective_at)
    )).scalars())
