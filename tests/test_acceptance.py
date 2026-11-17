"""Критерии приёмки из ТЗ, проверяемые на настоящей PostgreSQL."""
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from torgi.core import admin, exits, gifts, market, orders, placements, registry, trades, users
from torgi.core.errors import DomainError
from torgi.models import AuditLog, Holding, PriceHistory, Refund, Trade, TradeStatus as TS

R = 100  # 1 ₽ в копейках
pytestmark = pytest.mark.asyncio


async def member(app, tg_id, nick, payout=True):
    async with app.tx() as ctx:
        u = await users.get_or_create_tg(ctx, tg_id, nick)
        await users.accept_offer(ctx, u.id)
        await users.consent_pd(ctx, u.id)
        await users.set_profile(ctx, u.id, f"Иванов {nick} Петрович", "+7 900 000-00-00")
        if payout:
            await users.set_payout_details(ctx, u.id, "+79000000000", "Тинькофф")
        return u


async def founder(app):
    async with app.tx() as ctx:
        return await users.get_or_create_tg(ctx, 1, "founder")


async def open_placement(app, volume=25_000, price=100 * R):
    f = await founder(app)
    async with app.tx() as ctx:
        p = await placements.create(ctx, f, volume, price, days=30)
        await placements.publish(ctx, f, p.id)
    return p


async def buy_on_placement(app, u, shares):
    async with app.tx() as ctx:
        u = await users.get(ctx, u.id)
        pur, _ = await placements.reserve(ctx, u, shares)
    async with app.tx() as ctx:
        assert await placements.on_payment(ctx, pur.id, pur.amount) == "credited"
    return pur


async def shares_of(app, u):
    async with app.tx() as ctx:
        h = await registry.holding(ctx, u.id)
        return h.total, h.blocked


async def invariant(app):
    async with app.tx() as ctx:
        await registry.check_invariant(ctx)


async def setup_market(app):
    """Размещение, 10 «фоновых» участников по 2000 акций и 8 участников из примера ТЗ."""
    await open_placement(app)
    for i in range(10):
        await buy_on_placement(app, await member(app, 1000 + i, f"bg{i}"), 2000)
    people = {}
    for i, name in enumerate("АБВГДЕЖЗ"):
        people[name] = await member(app, 100 + i, f"u{i}")
    for name, qty in {"А": 20, "Б": 30, "В": 25, "Г": 15}.items():
        await buy_on_placement(app, people[name], qty if qty % 5 == 0 else qty)
    return people


async def place_example_orders(app, p):
    spec = [("А", "sell", 20, 110), ("Б", "sell", 30, 125), ("В", "sell", 25, 140), ("Г", "sell", 15, 160),
            ("Д", "buy", 10, 150), ("Е", "buy", 30, 135), ("Ж", "buy", 20, 125), ("З", "buy", 40, 115)]
    ids = {}
    for name, side, qty, price in spec:
        app.clock_obj.now += timedelta(seconds=1)
        async with app.tx() as ctx:
            u = await users.get(ctx, p[name].id)
            ids[name] = (await orders.create(ctx, u, side, qty, price * R)).id
    return ids


async def test_example_live_exchange(app):
    """Заявки из примера ТЗ, поданные по очереди, исполняются сразу по цене заявки из стакана."""
    p = await setup_market(app)
    ids = await place_example_orders(app, p)
    async with app.tx() as ctx:
        rows = (await ctx.s.execute(select(Trade).order_by(Trade.id))).scalars().all()
        name = {u.id: n for n, u in p.items()}
        assert [(name[t.buyer_id], name[t.seller_id], t.shares, t.price // R) for t in rows] == [
            ("Д", "А", 10, 110), ("Е", "А", 10, 110), ("Е", "Б", 20, 125), ("Ж", "Б", 10, 125)]
        assert all(t.pay_deadline == t.created_at + timedelta(hours=24) for t in rows)
        # цена SBAID — цена последней сделки
        assert (await orders.reference_price(ctx)) == 125 * R
        from torgi.models import Order
        st = {n: ((o := await ctx.s.get(Order, i)).status, o.filled) for n, i in ids.items()}
        assert st == {"А": ("filled", 20), "Б": ("filled", 30), "В": ("active", 0), "Г": ("active", 0),
                      "Д": ("filled", 10), "Е": ("filled", 30), "Ж": ("partial", 10), "З": ("active", 0)}
        asks, bids = await market.book(ctx)
        assert [(l.price // R, l.shares) for l in asks] == [(140, 25), (160, 15)]
        assert [(l.price // R, l.shares) for l in bids] == [(125, 10), (115, 40)]
        q = await market.quote(ctx)
        assert (q.last, q.best_bid, q.best_ask, q.trades_24h, q.volume_24h) == (125 * R, 125 * R, 140 * R, 4, 50)
        assert round(q.change_pct) == 25  # от цены размещения 100 ₽
    # SBAID не перенесены до подтверждения, но заблокированы у продавцов
    assert await shares_of(app, p["А"]) == (20, 20)
    assert await shares_of(app, p["Д"]) == (0, 0)
    assert any("вы покупаете 10 SBAID по 110 ₽" in m for m in app.notifier.to(p["Д"].id))
    assert any("вы продаёте 10 SBAID по 110 ₽" in m for m in app.notifier.to(p["А"].id))
    await invariant(app)


async def test_price_then_time_priority(app):
    p = await setup_market(app)
    for name, price in (("А", 120), ("Б", 120), ("В", 118)):
        app.clock_obj.now += timedelta(seconds=1)
        async with app.tx() as ctx:
            await orders.create(ctx, await users.get(ctx, p[name].id), "sell", 10, price * R)
    async with app.tx() as ctx:
        o = await orders.create(ctx, await users.get(ctx, p["Д"].id), "buy", 25, 130 * R)
        name = {u.id: n for n, u in p.items()}
        # сначала лучшая цена (В, 118 ₽), затем при равной цене — более ранняя заявка (А раньше Б)
        assert [(name[t.seller_id], t.shares, t.price // R) for t in o.new_trades] == [
            ("В", 10, 118), ("А", 10, 120), ("Б", 5, 120)]
        assert o.status == "filled"


async def test_no_cross_rests_in_book(app):
    p = await setup_market(app)
    async with app.tx() as ctx:
        before = await orders.reference_price(ctx)
        s = await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 10, 190 * R)
        b = await orders.create(ctx, await users.get(ctx, p["Д"].id), "buy", 10, 50 * R)
        assert s.new_trades == [] and b.new_trades == []
    async with app.tx() as ctx:
        assert await orders.reference_price(ctx) == before
        assert (await ctx.s.execute(select(Trade))).first() is None
        assert "190 ₽ — 10 SBAID" in await market.market_text(ctx)


async def test_duplicate_payment_notification(app):
    await open_placement(app)
    u = await member(app, 500, "buyer")
    async with app.tx() as ctx:
        pur, url = await placements.reserve(ctx, await users.get(ctx, u.id), 10)
        assert "auth.robokassa.ru" in url and "Receipt=" in url
    for expected in ("credited", "duplicate", "duplicate"):
        async with app.tx() as ctx:
            assert await placements.on_payment(ctx, pur.id, pur.amount) == expected
    assert await shares_of(app, u) == (10, 0)
    await invariant(app)


async def test_robokassa_signature(app):
    rk = app.payments
    import hashlib
    sig = hashlib.md5(b"1000.00:7:p2").hexdigest().upper()
    assert rk.verify_result({"OutSum": "1000.00", "InvId": "7", "SignatureValue": sig}).amount == 100_000
    assert rk.verify_result({"OutSum": "1000.00", "InvId": "7", "SignatureValue": "bad"}) is None


async def test_no_transfer_without_seller_confirmation(app):
    p = await setup_market(app)
    await place_example_orders(app, p)
    async with app.tx() as ctx:
        t = (await ctx.s.execute(select(Trade).order_by(Trade.id))).scalars().first()  # Д ← А, 10
        await trades.buyer_paid(ctx, await users.get(ctx, p["Д"].id), t.id)
    # продавец молчит, срок подтверждения прошёл — спор, акции не переносятся
    app.clock_obj.now = t.confirm_deadline + timedelta(minutes=1)
    async with app.tx() as ctx:
        await trades.process_deadlines(ctx)
        t = await ctx.s.get(Trade, t.id)
        assert t.status == TS.DISPUTED
    assert (await shares_of(app, p["Д"]))[0] == 0
    # покупатель Е не оплатил вовремя обе сделки по одной заявке — обе отменены, штрафов нет
    async with app.tx() as ctx:
        other = (await ctx.s.execute(select(Trade).where(Trade.status == TS.CANCELLED))).scalars().all()
        assert {x.buyer_id for x in other} >= {p["Е"].id}
        assert all(x.status == TS.CANCELLED for x in other if x.buyer_id == p["Е"].id)
    assert await shares_of(app, p["Б"]) == (30, 0)  # отменённые сделки (Е и Ж) разблокированы
    # админ решает спор в пользу переноса
    f = await founder(app)
    async with app.tx() as ctx:
        d = (await trades.open_disputes(ctx))[0]
        with pytest.raises(DomainError):
            await trades.resolve_dispute(ctx, f, d.id, "transfer", "")
    async with app.tx() as ctx:
        await trades.resolve_dispute(ctx, f, d.id, "transfer", "квитанция подтверждает перевод")
    assert (await shares_of(app, p["Д"]))[0] == 10
    await invariant(app)


async def test_seller_confirm_transfers(app):
    p = await setup_market(app)
    await place_example_orders(app, p)
    async with app.tx() as ctx:
        t = (await ctx.s.execute(select(Trade).order_by(Trade.id))).scalars().first()
        await trades.buyer_paid(ctx, await users.get(ctx, p["Д"].id), t.id)
    async with app.tx() as ctx:
        with pytest.raises(DomainError):  # покупатель не может подтвердить за продавца
            await trades.seller_confirm(ctx, await users.get(ctx, p["Д"].id), t.id)
    async with app.tx() as ctx:
        await trades.seller_confirm(ctx, await users.get(ctx, p["А"].id), t.id)
    assert await shares_of(app, p["Д"]) == (10, 0)
    assert await shares_of(app, p["А"]) == (10, 10)  # вторая сделка А ещё открыта
    assert any("Уступка" in m for m in app.notifier.to(p["Д"].id))
    await invariant(app)


async def test_founder_and_linked_cannot_order(app):
    p = await setup_market(app)
    f = await founder(app)
    async with app.tx() as ctx:
        with pytest.raises(DomainError):
            await orders.create(ctx, f, "sell", 10, 100 * R)
    async with app.tx() as ctx:
        await admin.set_linked(ctx, f, "u0", True)
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="связанные"):
            await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 5, 100 * R)


async def test_no_simultaneous_buy_and_sell(app):
    p = await setup_market(app)
    async with app.tx() as ctx:
        await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 5, 100 * R)
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="одновременно"):
            await orders.create(ctx, await users.get(ctx, p["А"].id), "buy", 5, 100 * R)


async def test_no_ownership_limit_by_default(app):
    p = await setup_market(app)
    async with app.tx() as ctx:  # 6000 из 25 000 — больше 15%, но лимит выключен
        await orders.create(ctx, await users.get(ctx, p["Д"].id), "buy", 6000, 100 * R)
    async with app.tx() as ctx:
        g = await gifts.offer(ctx, await users.by_username(ctx, "bg0"), "u4", 1000)
    async with app.tx() as ctx:
        await gifts.accept(ctx, await users.get(ctx, p["Д"].id), g.id)
    assert await shares_of(app, p["Д"]) == (1000, 0)


async def test_ownership_limit_on_buy_and_gift(app):
    """Лимит владения, если основатель его включит (15%)."""
    from torgi.models import SettingChange
    p = await setup_market(app)  # база лимита — партия 25 000 → 15% = 3750
    async with app.tx() as ctx:
        ctx.s.add(SettingChange(key="ownership_limit_pct", value={"v": 15}, published_at=ctx.now,
                                effective_at=ctx.now))
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="лимит владения"):
            await orders.create(ctx, await users.get(ctx, p["Д"].id), "buy", 3751, 100 * R)
    async with app.tx() as ctx:
        await orders.create(ctx, await users.get(ctx, p["Д"].id), "buy", 3000, 100 * R)
    # bg0 (2000 акций) дарит Д 1000 → с учётом заявки на 3000 лимит превышен
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="лимит владения"):
            await gifts.offer(ctx, await users.by_username(ctx, "bg0"), "u4", 1000)
    async with app.tx() as ctx:
        g = await gifts.offer(ctx, await users.by_username(ctx, "bg0"), "u5", 500)
    async with app.tx() as ctx:
        await gifts.accept(ctx, await users.get(ctx, p["Е"].id), g.id)
    assert await shares_of(app, p["Е"]) == (500, 0)
    await invariant(app)


async def test_placement_lot_and_limits(app):
    await open_placement(app)
    u = await member(app, 600, "x")
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="кратно"):
            await placements.reserve(ctx, await users.get(ctx, u.id), 7)
    async with app.tx() as ctx:  # лимита в одни руки по умолчанию нет
        pur, _ = await placements.reserve(ctx, await users.get(ctx, u.id), 3000)
        assert pur.shares == 3000
        assert placements.limit_text(await placements.current(ctx)) == ""


async def test_reservation_expires(app):
    await open_placement(app)
    u = await member(app, 600, "x")
    async with app.tx() as ctx:
        pur, _ = await placements.reserve(ctx, await users.get(ctx, u.id), 10)
    app.clock_obj.now += timedelta(minutes=31)
    async with app.tx() as ctx:
        assert await placements.expire_reservations(ctx) == 1
    # поздняя оплата при наличии акций всё равно зачисляется
    async with app.tx() as ctx:
        assert await placements.on_payment(ctx, pur.id, pur.amount) == "credited"
    assert await shares_of(app, u) == (10, 0)


async def test_refund_rules(app):
    await open_placement(app)
    u = await member(app, 700, "leaver")
    await buy_on_placement(app, u, 100)
    # автоматического возврата нет: даже на следующий день — только заявление основателю
    async with app.tx() as ctx:
        rs = await exits.request(ctx, await users.get(ctx, u.id), 30)
        assert [(r.kind, r.status, r.amount, r.max_amount) for r in rs] == [("admin", "pending_admin", None, 30 * 100 * R)]
    assert await shares_of(app, u) == (100, 30)  # заблокированы до решения
    f = await founder(app)
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="больше уплаченной"):
            await exits.decide(ctx, f, rs[0].id, 30 * 100 * R + 1, "")
    async with app.tx() as ctx:
        await exits.decide(ctx, f, rs[0].id, 25 * 100 * R, "за вычетом расходов")
    assert await shares_of(app, u) == (70, 0)
    # отказ возвращает SBAID владельцу
    async with app.tx() as ctx:
        rs2 = await exits.request(ctx, await users.get(ctx, u.id), 10)
    async with app.tx() as ctx:
        await exits.decide(ctx, f, rs2[0].id, None, "заявление отозвано")
    assert await shares_of(app, u) == (70, 0)
    # база тоже не даст записать возврат больше уплаченного
    async with app.tx() as ctx:
        r = await ctx.s.get(Refund, rs[0].id)
        r.amount = r.max_amount + 1
        with pytest.raises(Exception):
            await ctx.s.flush()
    await invariant(app)


async def test_market_shares_not_refundable(app):
    p = await setup_market(app)
    # Д ничего не покупал на размещении, получит акции подарком
    async with app.tx() as ctx:
        g = await gifts.offer(ctx, await users.by_username(ctx, "bg1"), "u4", 50)
    async with app.tx() as ctx:
        await gifts.accept(ctx, await users.get(ctx, p["Д"].id), g.id)
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="купленные на размещении"):
            await exits.request(ctx, await users.get(ctx, p["Д"].id), 10)


async def test_requisites_visible_only_to_paired_buyer(app):
    p = await setup_market(app)
    await place_example_orders(app, p)
    async with app.tx() as ctx:
        t = (await ctx.s.execute(select(Trade).order_by(Trade.id))).scalars().first()  # Д ← А
        assert "Тинькофф" in await trades.seller_requisites(ctx, await users.get(ctx, p["Д"].id), t.id)
        for other in ("Е", "А", "З"):
            with pytest.raises(DomainError):
                await trades.seller_requisites(ctx, await users.get(ctx, p[other].id), t.id)
    async with app.tx() as ctx:
        await trades.seller_confirm(ctx, await users.get(ctx, p["А"].id), t.id)
    async with app.tx() as ctx:  # после завершения сделки — уже нет
        with pytest.raises(DomainError):
            await trades.seller_requisites(ctx, await users.get(ctx, p["Д"].id), t.id)


async def test_journal_is_append_only_and_admin_actions_audited(app):
    p = await setup_market(app)
    f = await founder(app)
    async with app.tx() as ctx:
        await admin.adjust(ctx, f, "u0", -5, "ошибка при зачислении")
        await admin.set_linked(ctx, f, "u1", True)
        await admin.broadcast(ctx, f, "привет")
    async with app.tx() as ctx:
        actions = set((await ctx.s.execute(select(AuditLog.action).where(AuditLog.actor_id == f.id))).scalars())
        assert {"placement.create", "placement.publish", "balance.adjust", "user.linked", "broadcast"} <= actions
    with pytest.raises(Exception, match="только для добавления"):
        async with app.tx() as ctx:
            await ctx.s.execute(text("DELETE FROM transfers"))
    assert await shares_of(app, p["А"]) == (15, 0)
    await invariant(app)


async def test_order_price_band_and_expiry(app):
    p = await setup_market(app)
    async with app.tx() as ctx:
        # сделок ещё не было — цена SBAID равна цене размещения
        assert await orders.reference_price(ctx) == 100 * R
        with pytest.raises(DomainError, match="пределах"):
            await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 5, 201 * R)
        with pytest.raises(DomainError, match="от 1 до 30 дней"):
            await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 5, 150 * R, days=31)
        o = await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 5, 150 * R, days=1)
    assert await shares_of(app, p["А"]) == (20, 5)
    app.clock_obj.now += timedelta(days=1, seconds=1)
    async with app.tx() as ctx:
        # истёкшая заявка уже не исполняется, даже если её ещё не сняли
        b = await orders.create(ctx, await users.get(ctx, p["Д"].id), "buy", 5, 160 * R)
        assert b.new_trades == []
    async with app.tx() as ctx:
        assert await orders.expire(ctx) == 1
        from torgi.models import Order
        assert (await ctx.s.get(Order, o.id)).status == "expired"
    assert await shares_of(app, p["А"]) == (20, 0)


async def test_daily_summary_once_and_only_with_trades(app):
    p = await setup_market(app)
    app.clock_obj.now = app.clock_obj.now.replace(hour=18, minute=0)  # 21:00 МСК
    async with app.tx() as ctx:
        assert await market.daily_summary(ctx) is False  # сделок не было — не публикуем
    app.clock_obj.now += timedelta(days=1)
    app.clock_obj.now = app.clock_obj.now.replace(hour=9)  # 12:00 МСК
    await place_example_orders(app, p)
    async with app.tx() as ctx:
        assert await market.daily_summary(ctx) is False  # ещё рано
    app.clock_obj.now = app.clock_obj.now.replace(hour=18, minute=5)
    async with app.tx() as ctx:
        assert await market.daily_summary(ctx) is True
    async with app.tx() as ctx:
        assert await market.daily_summary(ctx) is False
    posts = [t for k, _, t in app.notifier.sent if k == "channel" and "Биржа SBAID" in t]
    assert len(posts) == 1
    assert "Цена: 125 ₽ (+25.0% за день)" in posts[0] and "Сделок: 4, объём 50 SBAID" in posts[0]
    assert "Мин. 110 ₽, макс. 125 ₽" in posts[0]


async def test_settings_take_effect_after_7_days(app):
    from torgi.core import settings
    f = await founder(app)
    async with app.tx() as ctx:
        await settings.change(ctx, f.id, "ownership_limit_pct", 20)
    async with app.tx() as ctx:
        assert await settings.get(ctx, "ownership_limit_pct") == 0
    app.clock_obj.now += timedelta(days=7, seconds=1)
    async with app.tx() as ctx:
        assert await settings.get(ctx, "ownership_limit_pct") == 20
    assert any(k == "channel" and "Вступает в силу" in t for k, _, t in app.notifier.sent)


async def test_voting_share_excludes_treasury(app):
    p = await setup_market(app)
    async with app.tx() as ctx:
        assert await registry.outstanding(ctx) == 20_090
        assert abs(await registry.voting_share(ctx, p["А"].id) - 20 / 20_090) < 1e-12
        total = (await ctx.s.execute(select(Holding.total))).scalars().all()
        assert sum(total) == 1_000_000


async def test_placement_cap_per_half_year(app):
    f = await founder(app)
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="не больше 25000"):
            await placements.create(ctx, f, 25_005, 100 * R, days=30)
    p = await open_placement(app)
    async with app.tx() as ctx:
        await placements.close(ctx, f, p.id)
        with pytest.raises(DomainError, match="не больше 25000"):
            await placements.create(ctx, f, 5, 100 * R, days=30)
    app.clock_obj.now += timedelta(days=183)
    async with app.tx() as ctx:
        await placements.create(ctx, f, 25_000, None, days=30)


async def test_trial_mode_manual_payment(app):
    from torgi.payments.robokassa import Robokassa
    await open_placement(app)
    u = await member(app, 800, "trial")
    f = await founder(app)
    async with app.tx() as ctx:
        pur, _ = await placements.reserve(ctx, await users.get(ctx, u.id), 10)
        with pytest.raises(DomainError, match="Robokassa подключена"):
            await placements.test_payment(ctx, f, pur.id)
    app.payments = Robokassa("", "", "")  # Robokassa ещё не подключена
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="только основателю"):
            await placements.test_payment(ctx, await users.get(ctx, u.id), pur.id)
    async with app.tx() as ctx:
        assert await placements.test_payment(ctx, f, pur.id) == "credited"
    async with app.tx() as ctx:
        assert await placements.test_payment(ctx, f, pur.id) == "duplicate"
    assert await shares_of(app, u) == (10, 0)


async def test_ladder_price_in_placement(app):
    """Лесенка по умолчанию выключена; проверяем, что включённая считается верно."""
    from torgi.models import SettingChange
    async with app.tx() as ctx:
        for k, v in (("ladder_every", 1000), ("ladder_step_kop", 200)):
            ctx.s.add(SettingChange(key=k, value={"v": v}, published_at=ctx.now, effective_at=ctx.now))
    await open_placement(app)  # 100 ₽, +2 ₽ за каждые 1000
    a, b = await member(app, 900, "lad_a"), await member(app, 901, "lad_b")
    p1 = await buy_on_placement(app, a, 995)
    assert p1.amount == 995 * 100 * R
    async with app.tx() as ctx:
        pur, _ = await placements.reserve(ctx, await users.get(ctx, b.id), 10)
        # 996-я…1000-я по 100 ₽, 1001-я…1005-я по 102 ₽
        assert pur.amount == 5 * 100 * R + 5 * 102 * R
        assert await placements.next_unit_price(ctx, await placements.current(ctx)) == 102 * R
    async with app.tx() as ctx:
        assert await placements.on_payment(ctx, pur.id, pur.amount) == "credited"
    f = await founder(app)
    async with app.tx() as ctx:
        p = await placements.current(ctx)
        await placements.close(ctx, f, p.id)
        # следующее размещение без сделок стартует с цены последнего проданного SBAID
        assert await placements.last_placement_price(ctx) == 102 * R
        assert await orders.reference_price(ctx) == 102 * R
    assert any("от 100 ₽ до 102 ₽" in t for k, _, t in app.notifier.sent if k == "channel")


async def open_auction(app, volume=25_000, min_price=None, days=7):
    f = await founder(app)
    async with app.tx() as ctx:
        p = await placements.create(ctx, f, volume, min_price, days=days, mode="auction")
        await placements.publish(ctx, f, p.id)
    return p


async def bid(app, u, shares, price_rub):
    app.clock_obj.now += timedelta(seconds=1)
    async with app.tx() as ctx:
        return await placements.place_bid(ctx, await users.get(ctx, u.id), shares, price_rub * R)


async def test_auction_uniform_price(app):
    p = await open_auction(app, volume=1000)
    a, b, c, d = [await member(app, 950 + i, f"auc{i}") for i in range(4)]
    await bid(app, a, 100, 150)
    await bid(app, b, 100, 150)  # «замена»: та же сумма
    await bid(app, c, 100, 120)  # после — эта заявка заменит прежнюю
    await bid(app, c, 100, 137)
    await bid(app, d, 100, 90)
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="аукцион"):
            await placements.reserve(ctx, await users.get(ctx, a.id), 5)
    app.clock_obj.now = p.ends_at + timedelta(minutes=1)
    async with app.tx() as ctx:
        await placements.close_expired(ctx)
    # спрос меньше партии: все выигрывают по цене самой низкой заявки
    async with app.tx() as ctx:
        from torgi.models import Purchase
        purs = (await ctx.s.execute(select(Purchase).order_by(Purchase.id))).scalars().all()
        assert [(x.user_id, x.shares, x.amount) for x in purs] == [
            (a.id, 100, 100 * 90 * R), (b.id, 100, 100 * 90 * R), (c.id, 100, 100 * 90 * R), (d.id, 100, 100 * 90 * R)]
        assert (await ctx.s.get(type(p), p.id)).price == 90 * R
    for pur in purs:
        async with app.tx() as ctx:
            assert await placements.on_payment(ctx, pur.id, pur.amount) == "credited"
    assert await shares_of(app, c) == (100, 0)
    async with app.tx() as ctx:
        assert await orders.reference_price(ctx) == 90 * R  # ниже 100 ₽ — допустимо
    await invariant(app)


async def test_auction_oversubscribed_cutoff(app):
    p = await open_auction(app, volume=250)
    async with app.tx() as ctx:  # маленькая партия: для теста поднимаем лимиты в одни руки и владения
        (await ctx.s.get(type(p), p.id)).per_user_limit = 100
        from torgi.models import SettingChange
        ctx.s.add(SettingChange(key="ownership_limit_pct", value={"v": 100}, published_at=ctx.now,
                                effective_at=ctx.now))
    a, b, c = [await member(app, 960 + i, f"ov{i}") for i in range(3)]
    await bid(app, a, 100, 160)
    await bid(app, b, 100, 140)
    await bid(app, c, 100, 120)
    app.clock_obj.now = p.ends_at + timedelta(minutes=1)
    async with app.tx() as ctx:
        await placements.close_expired(ctx)
        from torgi.models import PlacementBid, Purchase
        bids = {x.user_id: (x.status, x.allocated) for x in (await ctx.s.execute(select(PlacementBid))).scalars()}
        assert bids == {a.id: ("won", 100), b.id: ("won", 100), c.id: ("partial", 50)}
        purs = (await ctx.s.execute(select(Purchase))).scalars().all()
        assert {x.amount for x in purs} == {100 * 120 * R, 50 * 120 * R}  # все по 120 ₽
    assert any("Цена отсечения: 120 ₽" in t for k, _, t in app.notifier.sent if k == "channel")
    # неоплата за 48 часов — резерв снимается, SBAID остаются в казне
    app.clock_obj.now += timedelta(hours=49)
    async with app.tx() as ctx:
        assert await placements.expire_reservations(ctx) == 3
    await invariant(app)


async def test_auction_rules(app):
    await open_auction(app, volume=25_000, min_price=80 * R)
    u = await member(app, 970, "rules")
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="не меньше 80"):
            await placements.place_bid(ctx, await users.get(ctx, u.id), 10, 79 * R)
        with pytest.raises(DomainError, match="основателю|Основатель"):
            await placements.place_bid(ctx, await founder(app), 10, 100 * R)
    await bid(app, u, 10, 100)
    async with app.tx() as ctx:
        await placements.cancel_bid(ctx, await users.get(ctx, u.id))
        assert await placements.my_bid(ctx, (await placements.current(ctx)).id, u.id) is None
