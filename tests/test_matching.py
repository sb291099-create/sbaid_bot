from datetime import datetime, timedelta, timezone

from torgi.core.matching import OrderIn, clear
from torgi.core.pricing import next_placement_price, weighted_median

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
R = 100  # рубль в копейках


def _o(i, user, side, shares, price):
    return OrderIn(i, user, side, shares, price * R, T0 + timedelta(minutes=i))


EXAMPLE = [
    _o(1, "А", "sell", 20, 110), _o(2, "Б", "sell", 30, 125),
    _o(3, "В", "sell", 25, 140), _o(4, "Г", "sell", 15, 160),
    _o(5, "Д", "buy", 10, 150), _o(6, "Е", "buy", 30, 135),
    _o(7, "Ж", "buy", 20, 125), _o(8, "З", "buy", 40, 115),
]


def test_example_from_spec():
    c = clear(EXAMPLE, reference_price=120 * R)
    assert c.price == 125 * R
    assert c.volume == 50
    got = [(p.buyer_id, p.seller_id, p.shares, p.shares * c.price // R) for p in c.pairs]
    assert got == [("Д", "А", 10, 1250), ("Е", "А", 10, 1250), ("Е", "Б", 20, 2500), ("Ж", "Б", 10, 1250)]
    assert c.fills[7] == 10  # Ж урезан до 10 из 20
    assert 3 not in c.fills and 4 not in c.fills and 8 not in c.fills
    table = {p // R: (d, s, v) for p, d, s, v in c.table}
    assert table[110] == (100, 20, 20)
    assert table[135] == (40, 50, 40)
    assert table[160] == (0, 90, 0)


def test_no_cross_means_failed_day():
    c = clear([_o(1, "А", "sell", 10, 200), _o(2, "Б", "buy", 10, 100)], 150 * R)
    assert not c.happened and c.price is None and c.pairs == []


def test_empty():
    assert not clear([], 100).happened


def test_tie_breaks_closer_to_reference_then_lower():
    # объём 10 и дисбаланс 0 при ценах 100 и 120 — побеждает ближайшая к референсной
    orders = [_o(1, "А", "sell", 10, 100), _o(2, "Б", "buy", 10, 120)]
    assert clear(orders, 119 * R).price == 120 * R
    assert clear(orders, 101 * R).price == 100 * R
    assert clear(orders, 110 * R).price == 100 * R  # равноудалены — меньшая


def test_earlier_order_wins_at_equal_price():
    orders = [_o(1, "А", "sell", 10, 100), _o(3, "Б", "buy", 10, 100), _o(2, "В", "buy", 10, 100)]
    c = clear(orders, 100 * R)
    assert [p.buyer_id for p in c.pairs] == ["В"]


def test_weighted_median():
    assert weighted_median([(100, 1), (200, 5)]) == 200
    assert weighted_median([(100, 3), (200, 3)]) == 100


def _trades(price_rub, n=10):
    return [(price_rub * R, 1, i) for i in range(n)]


def test_placement_price_examples_from_spec():
    assert next_placement_price(100 * R, _trades(135)).price == 135 * R
    assert next_placement_price(100 * R, _trades(180)).price == 150 * R
    assert next_placement_price(100 * R, _trades(60)).price == 100 * R


def test_placement_price_needs_enough_trades():
    assert next_placement_price(100 * R, _trades(135, n=9)).price == 100 * R
    few_buyers = [(135 * R, 1, i % 4) for i in range(12)]
    assert next_placement_price(100 * R, few_buyers).price == 100 * R


def test_placement_price_rounds_to_5():
    assert next_placement_price(200 * R, _trades(213)).price == 215 * R
    assert next_placement_price(200 * R, _trades(212)).price == 210 * R
