"""Цена размещения (чистые функции)."""
from torgi.core.pricing import next_placement_price, weighted_median

R = 100  # рубль в копейках


def test_weighted_median():
    assert weighted_median([(100, 1), (200, 5)]) == 200
    assert weighted_median([(100, 3), (200, 3)]) == 100


def _trades(price_rub, n=10):
    return [(price_rub * R, 1, i) for i in range(n)]


def test_placement_price_is_market_price():
    # без коридора и нижней границы: стартовая цена = рыночная, даже ниже 100 ₽
    assert next_placement_price(100 * R, _trades(135)).price == 135 * R
    assert next_placement_price(100 * R, _trades(180)).price == 180 * R
    assert next_placement_price(100 * R, _trades(60)).price == 60 * R


def test_placement_price_optional_corridor_and_floor():
    kw = dict(down_pct=30, up_pct=50, floor=100 * R, step=500)
    assert next_placement_price(100 * R, _trades(180), **kw).price == 150 * R
    assert next_placement_price(100 * R, _trades(60), **kw).price == 100 * R


def test_placement_price_needs_enough_trades():
    assert next_placement_price(100 * R, _trades(135, n=9)).price == 100 * R
    few_buyers = [(135 * R, 1, i % 4) for i in range(12)]
    assert next_placement_price(100 * R, few_buyers).price == 100 * R


def test_placement_price_rounds_to_1_rub():
    trades = [(21_345, 1, i) for i in range(10)]  # 213,45 ₽
    assert next_placement_price(200 * R, trades).price == 213 * R


def test_ladder_from_proposal():
    from torgi.core.pricing import ladder_amount, ladder_price
    base, every, step = 100 * R, 1000, 2 * R
    assert [ladder_price(base, n, every, step) // R for n in (0, 999, 1000, 3000, 5000, 10000, 24999)] == \
        [100, 100, 102, 106, 110, 120, 148]
    # вся партия 25 000 SBAID: 3,1 млн ₽
    assert ladder_amount(base, 0, 25_000, every, step) == 3_100_000 * R
    # покупка через границу ступени: 990-я … 1009-я SBAID
    assert ladder_amount(base, 990, 20, every, step) == 10 * 100 * R + 10 * 102 * R
    assert ladder_amount(base, 0, 50, 0, 0) == 50 * 100 * R  # лесенка выключена
