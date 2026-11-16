"""Цена следующего размещения (чистая функция)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PriceCalc:
    price: int
    steps: list[str] = field(default_factory=list)
    median: int | None = None
    changed: bool = False


def weighted_median(observations: list[tuple[int, int]]) -> int:
    """Медиана, где каждая проданная акция — одно наблюдение.
    observations: [(цена, SBAID)]. При чётном числе — нижняя медиана."""
    obs = sorted(observations)
    n = sum(q for _, q in obs)
    if n == 0:
        raise ValueError("нет наблюдений")
    k = (n + 1) // 2  # номер медианного наблюдения (нижняя медиана)
    acc = 0
    for price, q in obs:
        acc += q
        if acc >= k:
            return price
    raise AssertionError


def round_to(value: float, step: int) -> int:
    # «до 5 ₽» — к ближайшему кратному, половина вверх
    return int((value + step / 2) // step * step)


def next_placement_price(prev: int, trades: list[tuple[int, int, int]], *, min_trades: int = 10,
                         min_buyers: int = 5, down_pct: int = 0, up_pct: int = 0,
                         floor: int = 0, step: int = 100) -> PriceCalc:
    """Стартовая цена размещения = рыночная: медиана цен завершённых сделок за окно,
    взвешенная по числу SBAID. trades: [(цена, SBAID, buyer_id)]. Деньги — в копейках.
    Коридор и нижняя граница по умолчанию выключены (0)."""
    from ..timeutil import rub
    calc = PriceCalc(price=prev)
    buyers = {b for _, _, b in trades}
    calc.steps.append(f"Прошлая цена: {rub(prev)}")
    calc.steps.append(f"Завершённых сделок за 30 дней: {len(trades)}, разных покупателей: {len(buyers)}")
    if len(trades) < min_trades or len(buyers) < min_buyers:
        calc.steps.append(f"Меньше {min_trades} сделок или {min_buyers} покупателей — цена остаётся прежней")
        return calc
    m = weighted_median([(p, q) for p, q, _ in trades])
    calc.median = m
    calc.steps.append(f"Рыночная цена (медиана, взвешенная по числу SBAID): {rub(m)}")
    value = float(m)
    if down_pct or up_pct:
        lo = prev * (100 - down_pct) / 100 if down_pct else 0
        hi = prev * (100 + up_pct) / 100 if up_pct else float("inf")
        bounded = min(hi, max(lo, value))
        if bounded != value:
            calc.steps.append(f"Коридор к прошлой цене: {rub(int(bounded))}")
        value = bounded
    if floor and value < floor:
        value = floor
        calc.steps.append(f"Не ниже {rub(floor)}: {rub(floor)}")
    p = max(step, round_to(value, step))
    if p != m:
        calc.steps.append(f"Округление до {rub(step)}: {rub(p)}")
    calc.price = p
    calc.changed = p != prev
    return calc


def ladder_price(base: int, index: int, every: int, step: int) -> int:
    """Цена SBAID с порядковым номером index (с нуля) внутри размещения."""
    if not every or not step:
        return base
    return base + step * (index // every)


def ladder_amount(base: int, start: int, count: int, every: int, step: int) -> int:
    """Сумма за count SBAID, начиная с порядкового номера start."""
    if not every or not step:
        return base * count
    total, i, end = 0, start, start + count
    while i < end:
        tier_end = min(end, (i // every + 1) * every)
        total += (tier_end - i) * ladder_price(base, i, every, step)
        i = tier_end
    return total
