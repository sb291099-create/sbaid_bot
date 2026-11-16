"""Сквозной прогон Telegram-слоя с поддельной сессией Bot API (без сети)."""
import hashlib
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import CreateChatInviteLink, EditMessageText, SendDocument, SendMessage
from aiogram.types import (CallbackQuery, Chat, ChatInviteLink, ChatMemberAdministrator, ChatMemberLeft,
                           ChatMemberUpdated, Message, Update, User as TgUser)
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from torgi.core import registry
from torgi.models import Trade, User
from torgi.scheduler import run_trading_day_if_due, tick
from torgi.telegram.bot import build_dispatcher
from torgi.telegram.notifier import TelegramNotifier
from torgi.web import build_web

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument, EditMessageText)):
            return Message(message_id=len(self.calls), date=NOW, chat=Chat(id=int(method.chat_id), type="private"),
                           text=getattr(method, "text", None))
        if isinstance(method, CreateChatInviteLink):
            return ChatInviteLink(invite_link="https://t.me/+one-time", creator=TgUser(id=42, is_bot=True, first_name="b"),
                                  creates_join_request=False, is_primary=False, is_revoked=False)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    def texts(self, chat_id):
        return [m.text for m in self.calls if isinstance(m, (SendMessage, EditMessageText)) and m.chat_id == chat_id]

    def last_markup(self, chat_id):
        for m in reversed(self.calls):
            if isinstance(m, SendMessage) and m.chat_id == chat_id and m.reply_markup:
                return m.reply_markup
        return None


class Harness:
    def __init__(self, app):
        self.app, self.session, self.n = app, FakeSession(), 0
        self.bot = Bot("123456:TEST", session=self.session)
        app.notifier = TelegramNotifier(self.bot, app.sm, app.config)
        self.dp = build_dispatcher(app)

    async def add_bot_as_admin(self, by_tg_id, chat_id, chat_type, title):
        self.n += 1
        actor = TgUser(id=by_tg_id, is_bot=False, first_name="x")
        me = TgUser(id=42, is_bot=True, first_name="bot")
        # все обязательные права — False, набор полей зависит от версии Bot API
        rights = {name: False for name, f in ChatMemberAdministrator.model_fields.items()
                  if f.is_required() and name != "user"}
        ev = ChatMemberUpdated(
            chat=Chat(id=chat_id, type=chat_type, title=title), from_user=actor, date=NOW,
            old_chat_member=ChatMemberLeft(user=me),
            new_chat_member=ChatMemberAdministrator(user=me, **rights))
        await self.dp.feed_update(self.bot, Update(update_id=self.n, my_chat_member=ev))

    async def say(self, tg_id, text, nick=None):
        self.n += 1
        u = TgUser(id=tg_id, is_bot=False, first_name="x", username=nick or f"user{tg_id}")
        msg = Message(message_id=self.n, date=NOW, chat=Chat(id=tg_id, type="private"), from_user=u, text=text)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, message=msg))
        return self.session.texts(tg_id)[-1] if self.session.texts(tg_id) else None

    async def click(self, tg_id, data, nick=None):
        self.n += 1
        u = TgUser(id=tg_id, is_bot=False, first_name="x", username=nick or f"user{tg_id}")
        msg = Message(message_id=self.n, date=NOW, chat=Chat(id=tg_id, type="private"), text="…")
        cq = CallbackQuery(id=str(self.n), from_user=u, chat_instance="c", data=data, message=msg)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, callback_query=cq))
        return self.session.texts(tg_id)[-1]

    async def register(self, tg_id):
        await self.say(tg_id, "/start")
        await self.click(tg_id, "reg:offer")
        await self.click(tg_id, "reg:pd")
        await self.say(tg_id, "Иванов Иван Иванович")
        return await self.say(tg_id, "ivan@example.com")

    async def pay(self, client, inv_id, amount_kop):
        out = f"{amount_kop / 100:.2f}"
        sig = hashlib.md5(f"{out}:{inv_id}:p2".encode()).hexdigest()
        r = await client.post("/robokassa/result", data={"OutSum": out, "InvId": str(inv_id), "SignatureValue": sig})
        return r.status, await r.text()


async def test_full_cycle_via_telegram(app):
    h = Harness(app)
    FOUNDER, SELLER, BUYER = 1, 10, 20
    assert "Принимаю" in h.session.last_markup(FOUNDER).inline_keyboard[0][0].text if await h.say(FOUNDER, "/start") else True
    assert "участник клуба" in await h.register(FOUNDER)
    assert "участник клуба" in await h.register(SELLER)
    assert "участник клуба" in await h.register(BUYER)

    # чужой человек не может подсунуть боту свой канал; основатель привязывает канал и группу
    await h.add_bot_as_admin(SELLER, -100999, "channel", "Чужой канал")
    await h.add_bot_as_admin(FOUNDER, -100100, "channel", "Сергей Борзунов · $SBAID")
    await h.add_bot_as_admin(FOUNDER, -100200, "supergroup", "SBAID Holders")
    assert "закрытая группа холдеров" in h.session.texts(FOUNDER)[-1]
    assert await app.notifier.chat_id("channel") == -100100 and await app.notifier.chat_id("group") == -100200

    # гость не может покупать; участник не может админить
    assert "зарегистрируйтесь" in await h.say(99, "/buy")
    assert "только основателю" in await h.say(SELLER, "/newplacement 25000 30")

    # основатель создаёт и публикует размещение
    assert "Черновик размещения №1" in await h.say(FOUNDER, "/newplacement 25000 30")
    assert "опубликовано" in await h.click(FOUNDER, "pl:publish:1")
    assert any("Размещение №1" in t for t in h.session.texts(-100100))

    # покупка на размещении, оплата через ResultURL, повторное уведомление
    async with TestClient(TestServer(build_web(app))) as client:
        assert "Сколько SBAID" in await h.say(SELLER, "/placement")
        assert "кратно" in await h.say(SELLER, "7")
        assert "Зарезервировано 100 SBAID" in await h.say(SELLER, "100")
        assert "auth.robokassa.ru" in h.session.last_markup(SELLER).inline_keyboard[0][0].url
        assert (await h.pay(client, 1, 100 * 100_00))[1] == "OK1"
        assert (await h.pay(client, 1, 100 * 100_00))[1] == "OK1"  # повтор — без второго начисления
        r = await client.post("/robokassa/result", data={"OutSum": "1.00", "InvId": "1", "SignatureValue": "x"})
        assert r.status == 400
        await h.say(BUYER, "/placement")
        await h.say(BUYER, "500")
        assert (await h.pay(client, 2, 500 * 100_00))[0] == 200
    assert any("одноразовая ссылка в закрытую группу «SBAID Holders»" in t for t in h.session.texts(SELLER))
    assert any("Сергей Борзунов · $SBAID" in t for t in h.session.texts(SELLER))
    assert "SBAID: 100 " in await h.say(SELLER, "/portfolio")

    # основатель не может подать заявку
    assert "не торгуют" in await h.say(FOUNDER, "/sell")

    # заявка на продажу: сначала реквизиты, потом количество, цена, срок
    assert "Куда принимать оплату" in await h.say(SELLER, "/sell")
    await h.say(SELLER, "+79001234567")
    await h.say(SELLER, "Сбер")
    await h.say(SELLER, "50")
    assert "срок" in await h.say(SELLER, "110")
    assert "Заявка №1 принята" in await h.click(SELLER, "ord:new:1")
    assert "одновременно" in await _try_buy(h, SELLER)

    await h.say(BUYER, "/buy")
    await h.say(BUYER, "30")
    await h.say(BUYER, "120")
    assert "Заявка №2 принята" in await h.click(BUYER, "ord:new:2")

    # торговый день по расписанию
    async with app.tx() as ctx:
        from torgi.core import orders
        cutoff = await orders.next_cutoff(ctx)
    app.clock_obj.now = cutoff + timedelta(minutes=1)
    assert await run_trading_day_if_due(app) is True
    assert await run_trading_day_if_due(app) is False
    assert any("Единая цена: 110 ₽" in t for t in h.session.texts(-100100))
    buyer_msgs = "\n".join(h.session.texts(BUYER))
    assert "+79001234567" in buyer_msgs and "Сбер" in buyer_msgs
    # реквизиты не уходят ни в канал, ни в группу, ни другим людям
    for chat in (-100100, -100200, FOUNDER):
        assert "+79001234567" not in "\n".join(t or "" for t in h.session.texts(chat))

    assert "Отметили оплату" in await h.click(BUYER, "tr:paid:1")
    assert "не ваша" in "".join(str(getattr(c, "text", "")) for c in await _alerts(h, BUYER, "tr:confirm:1"))
    assert "сделка завершена" in await h.click(SELLER, "tr:confirm:1")
    assert "SBAID: 530 " in await h.say(BUYER, "/portfolio")
    assert "SBAID: 70 (заблокировано 0)" in await h.say(SELLER, "/portfolio")  # остаток заявки истёк

    await tick(app)  # планировщик отрабатывает без ошибок
    async with app.tx() as ctx:
        await registry.check_invariant(ctx)
        t = await ctx.s.get(Trade, 1)
        assert t.status == "completed"
        founder = (await ctx.s.execute(select(User).where(User.telegram_id == FOUNDER))).scalar_one()
        assert founder.role == "founder"
    assert "placement.create" in await h.say(FOUNDER, "/journal")


async def _try_buy(h, tg_id):
    await h.say(tg_id, "/buy")
    await h.say(tg_id, "5")
    await h.say(tg_id, "100")
    before = len(h.session.calls)
    await h.click(tg_id, "ord:new:1")
    return "".join(str(getattr(c, "text", "")) for c in h.session.calls[before:])


async def _alerts(h, tg_id, data):
    before = len(h.session.calls)
    await h.click(tg_id, data)
    return h.session.calls[before:]
