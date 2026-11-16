"""Алгоритм торгового дня: единая цена и пары «покупатель → продавец».

Чистая функция без базы данных, чтобы её было легко проверить на примере из ТЗ.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OrderIn:
    id: int
    user_id: int
    side: str  # buy | sell
    shares: int  # остаток к исполнению
    limit_price: int
    created_at: datetime


@dataclass(frozen=True)
class Pair:
    buy_order_id: int
    sell_order_id: int
    buyer_id: int
    seller_id: int
    shares: int


@dataclass
class Clearing:
    price: int | None
    volume: int
    pairs: list[Pair]
    fills: dict[int, int]  # order_id → исполнено акций
    table: list[tuple[int, int, int, int]]  # (цена, спрос, предложение, объём)

    @property
    def happened(self) -> bool:
        return self.volume > 0


def clear(orders: list[OrderIn], reference_price: int) -> Clearing:
    buys = [o for o in orders if o.side == "buy" and o.shares > 0]
    sells = [o for o in orders if o.side == "sell" and o.shares > 0]
    prices = sorted({o.limit_price for o in orders if o.shares > 0})

    table = []
    for p in prices:
        demand = sum(o.shares for o in buys if o.limit_price >= p)
        supply = sum(o.shares for o in sells if o.limit_price <= p)
        table.append((p, demand, supply, min(demand, supply)))

    if not table:
        return Clearing(None, 0, [], {}, table)

    # наибольший объём; затем меньший дисбаланс; затем ближе к референсной; затем меньшая цена
    best = min(table, key=lambda r: (-r[3], abs(r[1] - r[2]), abs(r[0] - reference_price), r[0]))
    p_star, _, _, volume = best
    if volume == 0:
        return Clearing(None, 0, [], {}, table)

    # приоритет: лучшая цена, при равной — более ранняя заявка
    exec_buys = sorted((o for o in buys if o.limit_price >= p_star),
                       key=lambda o: (-o.limit_price, o.created_at, o.id))
    exec_sells = sorted((o for o in sells if o.limit_price <= p_star),
                        key=lambda o: (o.limit_price, o.created_at, o.id))

    buy_alloc = _allocate(exec_buys, volume)
    sell_alloc = _allocate(exec_sells, volume)

    pairs: list[Pair] = []
    bi = si = 0
    b_left = buy_alloc[0][1] if buy_alloc else 0
    s_left = sell_alloc[0][1] if sell_alloc else 0
    while bi < len(buy_alloc) and si < len(sell_alloc):
        bo, so = buy_alloc[bi][0], sell_alloc[si][0]
        q = min(b_left, s_left)
        pairs.append(Pair(bo.id, so.id, bo.user_id, so.user_id, q))
        b_left -= q
        s_left -= q
        if b_left == 0:
            bi += 1
            b_left = buy_alloc[bi][1] if bi < len(buy_alloc) else 0
        if s_left == 0:
            si += 1
            s_left = sell_alloc[si][1] if si < len(sell_alloc) else 0

    fills = {o.id: q for o, q in buy_alloc + sell_alloc}
    return Clearing(p_star, volume, pairs, fills, table)


def _allocate(side: list[OrderIn], volume: int) -> list[tuple[OrderIn, int]]:
    """Сторона с избытком урезается с конца очереди (худшая цена, более поздняя заявка)."""
    out, left = [], volume
    for o in side:
        if left == 0:
            break
        q = min(o.shares, left)
        out.append((o, q))
        left -= q
    return out
