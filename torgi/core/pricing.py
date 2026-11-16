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
    observations: [(цена, акций)]. При чётном числе — нижняя медиана."""
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
                         min_buyers: int = 5, down_pct: int = 30, up_pct: int = 50,
                         floor: int = 10_000, step: int = 500) -> PriceCalc:
    """trades: [(цена, акций, buyer_id)] завершённых сделок за окно. Деньги — в копейках."""
    from ..timeutil import rub
    calc = PriceCalc(price=prev)
    buyers = {b for _, _, b in trades}
    calc.steps.append(f"Прошлая цена: {rub(prev)}")
    calc.steps.append(f"Завершённых сделок за окно: {len(trades)}, разных покупателей: {len(buyers)}")
    if len(trades) < min_trades or len(buyers) < min_buyers:
        calc.steps.append(f"Меньше {min_trades} сделок или {min_buyers} покупателей — цена остаётся прежней")
        return calc
    m = weighted_median([(p, q) for p, q, _ in trades])
    calc.median = m
    calc.steps.append(f"Медиана, взвешенная по числу акций: {rub(m)}")
    lo, hi = prev * (100 - down_pct) / 100, prev * (100 + up_pct) / 100
    bounded = min(hi, max(lo, m))
    if bounded != m:
        calc.steps.append(f"Коридор {rub(int(lo))} … {rub(int(hi))}: {rub(int(bounded))}")
    floored = max(floor, bounded)
    if floored != bounded:
        calc.steps.append(f"Не ниже {rub(floor)}: {rub(int(floored))}")
    p = round_to(floored, step)
    calc.steps.append(f"Округление до {rub(step)}: {rub(p)}")
    calc.price = p
    calc.changed = p != prev
    return calc
