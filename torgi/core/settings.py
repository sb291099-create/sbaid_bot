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
    "placement_lot": (5, "Лот на размещении, SBAID"),
    "placement_period_cap": (25_000, "Максимум SBAID на размещениях за период"),
    "placement_period_days": (182, "Период для лимита размещений, дней"),
    "placement_user_limit_pct": (0, "Лимит в одни руки на размещении, % объёма партии (0 — без лимита)"),
    "ownership_limit_pct": (0, "Лимит владения, % всех размещённых SBAID (0 — без лимита)"),
    "reserve_minutes": (30, "Резерв SBAID на время оплаты, минут"),
    "trade_pay_hours": (24, "Часов покупателю на перевод денег после сделки"),
    "trade_confirm_hours": (24, "Часов продавцу на подтверждение после срока оплаты"),
    "dispute_days": (3, "Срок решения спора, дней"),
    "order_price_band_pct": (100, "Коридор цены в заявке, ± % от последней сделки"),
    "order_max_days": (30, "Максимальный срок жизни заявки, дней"),
    "summary_hour": (21, "Час ежедневной сводки торгов в канале, МСК"),
    "reminder_hours": (6, "Напоминание до срока, часов"),
    "price_window_days": (30, "Окно расчёта цены размещения, дней"),
    "price_min_trades": (10, "Минимум сделок для пересчёта цены размещения"),
    "price_min_buyers": (5, "Минимум разных покупателей для пересчёта цены"),
    "price_corridor_down_pct": (0, "Коридор цены размещения вниз, % (0 — без коридора)"),
    "price_corridor_up_pct": (0, "Коридор цены размещения вверх, % (0 — без коридора)"),
    "price_floor_kop": (0, "Нижняя граница цены размещения, коп. (0 — без границы)"),
    "price_step_kop": (100, "Шаг округления цены размещения, коп."),
    "auction_pay_hours": (48, "Аукцион размещения: часов на оплату после итогов"),
    "ladder_every": (0, "Лесенка: цена растёт после каждых N проданных SBAID (0 — одна цена)"),
    "ladder_step_kop": (0, "Лесенка: на сколько растёт цена за ступень, коп. (0 — одна цена)"),
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
