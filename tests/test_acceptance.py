"""Критерии приёмки из ТЗ, проверяемые на настоящей PostgreSQL."""
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from torgi.core import admin, exits, gifts, orders, placements, registry, trades, trading, users
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


async def run_day(app):
    async with app.tx() as ctx:
        cutoff = await orders.next_cutoff(ctx)
    app.clock_obj.now = cutoff
    async with app.tx() as ctx:
        return await trading.run(ctx, cutoff)


async def test_example_trading_day(app):
    p = await setup_market(app)
    await place_example_orders(app, p)
    sess = await run_day(app)
    assert sess.status == "done" and sess.price == 125 * R and sess.volume == 50 and sess.trades_count == 4
    async with app.tx() as ctx:
        rows = (await ctx.s.execute(select(Trade).order_by(Trade.id))).scalars().all()
        name = {u.id: n for n, u in p.items()}
        assert [(name[t.buyer_id], name[t.seller_id], t.shares, t.amount // R) for t in rows] == [
            ("Д", "А", 10, 1250), ("Е", "А", 10, 1250), ("Е", "Б", 20, 2500), ("Ж", "Б", 10, 1250)]
        assert (await orders.reference_price(ctx)) == 125 * R
    # акции не перенесены до подтверждения, но заблокированы у продавцов
    assert await shares_of(app, p["А"]) == (20, 20)
    assert await shares_of(app, p["Д"]) == (0, 0)
    await invariant(app)


async def test_failed_day_keeps_price(app):
    p = await setup_market(app)
    async with app.tx() as ctx:
        await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 10, 190 * R)
        await orders.create(ctx, await users.get(ctx, p["Д"].id), "buy", 10, 50 * R)
        before = await orders.reference_price(ctx)
    sess = await run_day(app)
    assert sess.status == "failed" and sess.price is None
    async with app.tx() as ctx:
        assert await orders.reference_price(ctx) == before
        assert (await ctx.s.execute(select(PriceHistory).where(PriceHistory.source == "session"))).first() is None


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
    await run_day(app)
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
    # покупатель Е не оплатил вовремя — сделка отменена, ему 1 штрафной балл
    async with app.tx() as ctx:
        other = (await ctx.s.execute(select(Trade).where(Trade.status == TS.CANCELLED))).scalars().all()
        assert {x.buyer_id for x in other} >= {p["Е"].id}
        assert await users.penalty_points(ctx, p["Е"].id) == 1
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
    await run_day(app)
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


async def test_ownership_limit_on_buy_and_gift(app):
    p = await setup_market(app)  # база лимита — партия 25 000 → 15% = 3750
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
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="одни руки"):
            await placements.reserve(ctx, await users.get(ctx, u.id), 2505)


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
    await run_day(app)
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


async def test_ban_after_three_points(app):
    p = await setup_market(app)
    f = await founder(app)
    async with app.tx() as ctx:
        await admin.manual_penalty(ctx, f, "u0", 3, "тест")
    async with app.tx() as ctx:
        with pytest.raises(DomainError, match="запрещена"):
            await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 5, 100 * R)


async def test_order_price_band_and_expiry(app):
    p = await setup_market(app)
    async with app.tx() as ctx:
        # референсная — цена последнего проданного SBAID на размещении: 20 090 продано → 140 ₽
        assert await orders.reference_price(ctx) == 140 * R
        with pytest.raises(DomainError, match="пределах"):
            await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 5, 281 * R)
        o = await orders.create(ctx, await users.get(ctx, p["А"].id), "sell", 5, 150 * R)
    await run_day(app)  # не пересеклось → заявка истекла, акции разблокированы
    async with app.tx() as ctx:
        from torgi.models import Order
        assert (await ctx.s.get(Order, o.id)).status == "expired"
    assert await shares_of(app, p["А"]) == (20, 0)


async def test_settings_take_effect_after_7_days(app):
    from torgi.core import settings
    f = await founder(app)
    async with app.tx() as ctx:
        await settings.change(ctx, f.id, "ownership_limit_pct", 20)
    async with app.tx() as ctx:
        assert await settings.get(ctx, "ownership_limit_pct") == 15
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
