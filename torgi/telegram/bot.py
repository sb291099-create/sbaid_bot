"""Telegram-интерфейс: только разбор команд и кнопок. Вся логика — в torgi.core."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, ExceptionTypeFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, ChatMemberUpdated, ErrorEvent, Message

from ..core import admin, exits, gifts, orders, placements, registry, settings, trades, trading, users
from ..core.context import App, Button
from ..core.errors import DomainError
from ..models import Role, Trade, TradeStatus, User
from ..timeutil import fmt_msk, rub
from .notifier import markup

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")


class Reg(StatesGroup):
    full_name = State()
    contact = State()


class Buy(StatesGroup):
    placement_qty = State()
    auction_qty = State()
    auction_price = State()


class OrderFlow(StatesGroup):
    payout_phone = State()
    payout_bank = State()
    qty = State()
    price = State()


class GiftFlow(StatesGroup):
    nick = State()
    qty = State()


class ExitFlow(StatesGroup):
    qty = State()


class Proof(StatesGroup):
    file = State()


class UserMiddleware(BaseMiddleware):
    def __init__(self, app: App):
        self.app = app

    async def __call__(self, handler, event, data):
        tg = data.get("event_from_user")
        if tg and not tg.is_bot:
            async with self.app.tx() as ctx:
                data["user"] = await users.get_or_create_tg(ctx, tg.id, tg.username)
        data["app"] = self.app
        return await handler(event, data)


def parse_int(text: str | None) -> int:
    try:
        return int((text or "").strip().replace(" ", ""))
    except ValueError:
        raise DomainError("Введите целое число") from None


def parse_rub(text: str | None) -> int:
    try:
        v = Decimal((text or "").strip().replace(" ", "").replace(",", ".").rstrip("₽рР"))
    except InvalidOperation:
        raise DomainError("Введите цену в рублях, например 125") from None
    if v <= 0:
        raise DomainError("Цена должна быть положительной")
    return int((v * 100).to_integral_value())


SIDE = {"buy": "покупка", "sell": "продажа"}
ORDER_STATUS = {"active": "активна", "partial": "исполнена частично", "filled": "исполнена",
                "cancelled": "отменена", "expired": "истекла"}
TRADE_STATUS = {"awaiting_payment": "ждёт оплаты", "awaiting_confirmation": "ждёт подтверждения",
                "completed": "завершена", "cancelled": "отменена", "disputed": "спор"}

HELP = ("Команды:\n/placement — купить SBAID на размещении\n/buy, /sell — заявка на рынке\n"
        "/orders — мои заявки\n/trades — мои сделки\n/portfolio — портфель\n/gift — подарить SBAID\n"
        "/exit — выйти из клуба\n/rules — правила и оферта\n/linked — аккаунты, связанные с основателем\n"
        "/registry — публичный реестр")


# --- регистрация ---------------------------------------------------------------------
def legal_links(app: App) -> str:
    c = app.config
    return (f"• Оферта (версия {c.offer_version}): {c.offer_url or 'ссылка не настроена'}\n"
            f"• Правила торгов: {c.rules_url or 'ссылка не настроена'}\n"
            f"• Политика персональных данных: {c.privacy_url or 'ссылка не настроена'}")


@router.message(Command("start"))
async def start(m: Message, user: User, app: App, state: FSMContext):
    await state.clear()
    if users.is_registered(user):
        return await m.answer("Вы уже зарегистрированы.\n\n" + HELP)
    c = app.config
    channel = f"«{c.channel_name}»" + (f" ({c.channel_url})" if c.channel_url else "")
    await m.answer(
        f"Это клуб SBAID. Основатель — {c.founder_name}. 1 SBAID = 1 голос в его решениях: "
        f"вы решаете, он делает.\n\n"
        f"SBAID продаются на размещениях, а раз в неделю участники могут купить и продать их друг "
        f"другу. Новости и итоги торгов выходят в канале {channel}. После первой покупки бот "
        f"пришлёт личную ссылку в закрытую группу «{c.group_name}».\n\n"
        "SBAID — единица участия и голоса в клубе. Это не ценная бумага, не доля в имуществе "
        "и не инвестиция, дохода она не гарантирует.\n\n"
        + legal_links(app),
        reply_markup=markup([[Button("Принимаю оферту и правила", "reg:offer")]]))


@router.message(Command("rules"))
async def rules(m: Message, app: App):
    await m.answer(legal_links(app))


@router.callback_query(F.data == "reg:offer")
async def reg_offer(c: CallbackQuery, user: User, app: App):
    async with app.tx() as ctx:
        await users.accept_offer(ctx, user.id)
    await c.message.answer("Оферта принята. Теперь нужно согласие на обработку персональных данных "
                           "(ФИО и контакт нужны для договора уступки; реквизиты продавца передаются "
                           "только покупателю из его пары).",
                           reply_markup=markup([[Button("Даю согласие", "reg:pd")]]))
    await c.answer()


@router.callback_query(F.data == "reg:pd")
async def reg_pd(c: CallbackQuery, user: User, app: App, state: FSMContext):
    async with app.tx() as ctx:
        await users.consent_pd(ctx, user.id)
    await state.set_state(Reg.full_name)
    await c.message.answer("Напишите ваши ФИО полностью.")
    await c.answer()


@router.message(Reg.full_name, F.text, ~F.text.startswith("/"))
async def reg_name(m: Message, state: FSMContext):
    if len(m.text.split()) < 2:
        raise DomainError("Укажите ФИО полностью — оно нужно для договора уступки")
    await state.update_data(full_name=m.text)
    await state.set_state(Reg.contact)
    await m.answer("Теперь телефон или e-mail для связи.")


@router.message(Reg.contact, F.text, ~F.text.startswith("/"))
async def reg_contact(m: Message, user: User, app: App, state: FSMContext):
    data = await state.get_data()
    async with app.tx() as ctx:
        await users.set_profile(ctx, user.id, data["full_name"], m.text)
    await state.clear()
    await m.answer("Готово, вы участник клуба.\n\n" + HELP)


# --- размещение ----------------------------------------------------------------------
@router.message(Command("placement"))
async def placement(m: Message, user: User, app: App, state: FSMContext):
    async with app.tx() as ctx:
        p = await placements.current(ctx)
        if p is None:
            return await m.answer("Сейчас нет открытого размещения. Объявление появится в канале.")
        if p.mode == "auction":
            bid = await placements.my_bid(ctx, p.id, user.id)
            mine = (f"\n\nВаша заявка: {bid.shares} SBAID по цене до {rub(bid.max_price)}. Новая заявка "
                    f"заменит её.") if bid else ""
            await state.set_state(Buy.auction_qty)
            kb = markup([[Button("Отменить мою заявку", "pl:cancelbid")]]) if bid else None
            return await m.answer(
                f"Аукцион размещения №{p.number}: {p.volume} SBAID\n{placements.ladder_text(p)}\n"
                f"Лот: {p.lot}, лимит в одни руки: {p.per_user_limit}\nПриём заявок до {fmt_msk(p.ends_at)}. "
                f"Деньги списываются только после итогов, если заявка выиграла.{mine}\n\n"
                f"Сколько SBAID хотите купить? Число, кратное {p.lot}.", reply_markup=kb)
        left = await placements.available(ctx, p)
        mine = await placements.user_bought(ctx, p.id, user.id)
        now_price = await placements.next_unit_price(ctx, p)
    ladder = ""
    if p.ladder_every and p.ladder_step and left:
        taken = p.volume - left
        to_next = p.ladder_every - taken % p.ladder_every
        ladder = (f"\nЦена растёт на {rub(p.ladder_step)} после каждых {p.ladder_every} проданных SBAID: "
                  f"ещё {min(to_next, left)} SBAID по {rub(now_price)}, дальше {rub(now_price + p.ladder_step)}.")
    await state.set_state(Buy.placement_qty)
    await m.answer(f"Размещение №{p.number}\nЦена сейчас: {rub(now_price)} за 1 SBAID{ladder}\nОсталось: {left} из {p.volume}\n"
                   f"Лот: {p.lot} SBAID\nЛимит в одни руки: {p.per_user_limit} (у вас уже {mine})\n"
                   f"До {fmt_msk(p.ends_at)}\n\nСколько SBAID хотите купить? Напишите число, кратное {p.lot}.")


@router.message(Buy.auction_qty, F.text, ~F.text.startswith("/"))
async def auction_qty(m: Message, state: FSMContext):
    await state.update_data(qty=parse_int(m.text))
    await state.set_state(Buy.auction_price)
    await m.answer("Максимальная цена за 1 SBAID, ₽? Если цена аукциона окажется ниже, заплатите меньше: "
                   "все победители платят одну цену.")


@router.message(Buy.auction_price, F.text, ~F.text.startswith("/"))
async def auction_price(m: Message, user: User, app: App, state: FSMContext):
    data = await state.get_data()
    async with app.tx() as ctx:
        b = await placements.place_bid(ctx, await users.get(ctx, user.id), data["qty"], parse_rub(m.text))
        p = await placements.current(ctx)
    await state.clear()
    await m.answer(f"Заявка принята: {b.shares} SBAID по цене до {rub(b.max_price)}. Итоги — после "
                   f"{fmt_msk(p.ends_at)}, бот пришлёт результат и ссылку на оплату. Изменить заявку: /placement")


@router.callback_query(F.data == "pl:cancelbid")
async def auction_cancel(c: CallbackQuery, user: User, app: App, state: FSMContext):
    async with app.tx() as ctx:
        await placements.cancel_bid(ctx, await users.get(ctx, user.id))
    await state.clear()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Заявка на аукционе отменена.")
    await c.answer()


@router.message(Buy.placement_qty, F.text, ~F.text.startswith("/"))
async def placement_qty(m: Message, user: User, app: App, state: FSMContext):
    qty = parse_int(m.text)
    async with app.tx() as ctx:
        pur, url = await placements.reserve(ctx, await users.get(ctx, user.id), qty)
    await state.clear()
    kb = markup([[Button(f"Оплатить {rub(pur.amount)}", url=url)]]) if url.startswith("http") else None
    await m.answer(f"Зарезервировано {pur.shares} SBAID на сумму {rub(pur.amount)} до {fmt_msk(pur.reserved_until)}. "
                   f"SBAID будут начислены сразу после оплаты." + ("" if kb else f"\n{url}"), reply_markup=kb)


# --- заявки --------------------------------------------------------------------------
@router.message(Command("sell", "buy"))
async def order_start(m: Message, command: CommandObject, user: User, app: App, state: FSMContext):
    side = command.command
    async with app.tx() as ctx:
        u = await users.get(ctx, user.id)
        users.require_member(u)
        if users.founder_side(u):
            raise DomainError("Основатель и связанные с ним аккаунты не торгуют на рынке между участниками")
        ref = await orders.reference_price(ctx)
        h = await registry.holding(ctx, u.id)
    await state.update_data(side=side)
    if side == "sell" and not user.payout_details_enc:
        await state.set_state(OrderFlow.payout_phone)
        return await m.answer("Куда принимать оплату? Напишите телефон для перевода по СБП. "
                              "Его увидит только покупатель из вашей сделки.")
    await state.set_state(OrderFlow.qty)
    hint = f"Свободно SBAID: {h.total - h.blocked}. " if side == "sell" else ""
    await m.answer(f"Заявка на {SIDE[side]}у. Референсная цена: {rub(ref)}. {hint}Сколько SBAID?")


@router.message(OrderFlow.payout_phone, F.text, ~F.text.startswith("/"))
async def payout_phone(m: Message, state: FSMContext):
    await state.update_data(phone=m.text)
    await state.set_state(OrderFlow.payout_bank)
    await m.answer("В каком банке этот номер?")


@router.message(OrderFlow.payout_bank, F.text, ~F.text.startswith("/"))
async def payout_bank(m: Message, user: User, app: App, state: FSMContext):
    data = await state.get_data()
    async with app.tx() as ctx:
        await users.set_payout_details(ctx, user.id, data["phone"], m.text)
    await state.set_state(OrderFlow.qty)
    await m.answer("Реквизиты сохранены (в зашифрованном виде). Сколько SBAID продаёте?")


@router.message(OrderFlow.qty, F.text, ~F.text.startswith("/"))
async def order_qty(m: Message, state: FSMContext):
    await state.update_data(qty=parse_int(m.text))
    data = await state.get_data()
    await state.set_state(OrderFlow.price)
    await m.answer("Минимальная цена за 1 SBAID, ₽?" if data["side"] == "sell" else "Максимальная цена за 1 SBAID, ₽?")


@router.message(OrderFlow.price, F.text, ~F.text.startswith("/"))
async def order_price(m: Message, app: App, state: FSMContext):
    await state.update_data(price=parse_rub(m.text))
    async with app.tx() as ctx:
        max_weeks = await settings.get(ctx, "order_max_weeks")
    row = [Button("До ближайшего торгового дня", "ord:new:1")]
    more = [Button(f"{w} нед.", f"ord:new:{w}") for w in range(2, max_weeks + 1)]
    await m.answer("На какой срок заявка?", reply_markup=markup([row, more] if more else [row]))


@router.callback_query(F.data.startswith("ord:new:"))
async def order_create(c: CallbackQuery, user: User, app: App, state: FSMContext):
    data = await state.get_data()
    if not {"side", "qty", "price"} <= data.keys():
        raise DomainError("Начните заново: /buy или /sell")
    weeks = int(c.data.split(":")[2])
    async with app.tx() as ctx:
        o = await orders.create(ctx, await users.get(ctx, user.id), data["side"], data["qty"], data["price"], weeks)
    await state.clear()
    await c.message.edit_text(f"Заявка №{o.id} принята: {SIDE[o.side]} {o.shares} SBAID, цена "
                              f"{'не ниже' if o.side == 'sell' else 'не выше'} {rub(o.limit_price)}, "
                              f"действует до {fmt_msk(o.expires_at)}. Отменить можно в /orders до отсечки.")
    await c.answer()


@router.message(Command("orders"))
async def my_orders(m: Message, user: User, app: App):
    async with app.tx() as ctx:
        rows = await orders.active_orders(ctx, user.id)
        cutoff = await orders.next_cutoff(ctx)
    if not rows:
        return await m.answer(f"Активных заявок нет. Ближайший торговый день: {fmt_msk(cutoff)}.")
    for o in rows:
        await m.answer(f"№{o.id}: {SIDE[o.side]} {o.shares} SBAID по {rub(o.limit_price)}, исполнено {o.filled}, "
                       f"{ORDER_STATUS[o.status]}, до {fmt_msk(o.expires_at)}",
                       reply_markup=markup([[Button("Отменить", f"ord:cancel:{o.id}")]]))
    await m.answer(f"Ближайший торговый день: {fmt_msk(cutoff)}.")


@router.callback_query(F.data.startswith("ord:cancel:"))
async def order_cancel(c: CallbackQuery, user: User, app: App):
    async with app.tx() as ctx:
        o = await orders.cancel(ctx, await users.get(ctx, user.id), int(c.data.split(":")[2]))
    await c.message.edit_text(f"Заявка №{o.id} отменена.")
    await c.answer()


# --- сделки --------------------------------------------------------------------------
@router.message(Command("trades"))
async def my_trades(m: Message, user: User, app: App):
    async with app.tx() as ctx:
        rows = await trades.user_trades(ctx, user.id)
        cards = []
        for t in rows:
            role = "покупка" if t.buyer_id == user.id else "продажа"
            text = (f"Сделка №{t.id}: {role} {t.shares} SBAID по {rub(t.price)} = {rub(t.amount)}, "
                    f"{TRADE_STATUS[t.status]}")
            kb = []
            if t.status == TradeStatus.AWAITING_PAYMENT and t.buyer_id == user.id:
                req = await trades.seller_requisites(ctx, await users.get(ctx, user.id), t.id)
                text += f"\nОплатить до {fmt_msk(t.pay_deadline)}. Реквизиты: {req}"
                kb.append([Button("Я оплатил", f"tr:paid:{t.id}")])
            if t.status in (TradeStatus.AWAITING_PAYMENT, TradeStatus.AWAITING_CONFIRMATION) and t.seller_id == user.id:
                kb.append([Button("Деньги получил", f"tr:confirm:{t.id}")])
                kb.append([Button("Денег нет", f"tr:nomoney:{t.id}")])
            if t.status in TradeStatus.OPEN:
                kb.append([Button("Прислать доказательство", f"tr:proof:{t.id}")])
            cards.append((text, kb))
    if not cards:
        return await m.answer("Сделок пока нет.")
    for text, kb in cards:
        await m.answer(text, reply_markup=markup(kb))


@router.callback_query(F.data.startswith("tr:"))
async def trade_action(c: CallbackQuery, user: User, app: App, state: FSMContext):
    _, action, tid = c.data.split(":")
    tid = int(tid)
    if action == "proof":
        await state.set_state(Proof.file)
        await state.update_data(trade_id=tid)
        await c.message.answer("Пришлите фото или файл (скриншот перевода, квитанцию или выписку).")
        return await c.answer()
    async with app.tx() as ctx:
        u = await users.get(ctx, user.id)
        if action == "paid":
            await trades.buyer_paid(ctx, u, tid)
            reply = "Отметили оплату. Продавец получил уведомление; SBAID придут после его подтверждения."
        elif action == "confirm":
            await trades.seller_confirm(ctx, u, tid)
            reply = "Спасибо, сделка завершена, SBAID перенесены покупателю."
        elif action == "nomoney":
            await trades.seller_no_money(ctx, u, tid)
            reply = "Открыт спор. Пришлите выписку за день — админ разберётся."
        else:
            raise DomainError("Неизвестное действие")
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(reply)
    await c.answer()


@router.message(Proof.file, F.photo | F.document)
async def proof_file(m: Message, user: User, app: App, state: FSMContext):
    data = await state.get_data()
    file_id, kind = (m.photo[-1].file_id, "photo") if m.photo else (m.document.file_id, "document")
    async with app.tx() as ctx:
        await trades.add_evidence(ctx, await users.get(ctx, user.id), data["trade_id"], file_id, kind)
    await state.clear()
    await m.answer("Доказательство сохранено.")


# --- подарок, портфель, выход ---------------------------------------------------------
@router.message(Command("gift"))
async def gift_start(m: Message, user: User, state: FSMContext):
    users.require_member(user)
    await state.set_state(GiftFlow.nick)
    await m.answer("Кому дарим? Напишите ник получателя, например @ivan.")


@router.message(GiftFlow.nick, F.text, ~F.text.startswith("/"))
async def gift_nick(m: Message, state: FSMContext):
    await state.update_data(nick=m.text.strip())
    await state.set_state(GiftFlow.qty)
    await m.answer("Сколько SBAID?")


@router.message(GiftFlow.qty, F.text, ~F.text.startswith("/"))
async def gift_qty(m: Message, user: User, app: App, state: FSMContext):
    data = await state.get_data()
    qty = parse_int(m.text)
    async with app.tx() as ctx:
        g = await gifts.offer(ctx, await users.get(ctx, user.id), data["nick"], qty)
    await state.clear()
    await m.answer(f"Получателю отправлен запрос. {g.shares} SBAID заблокированы до его ответа.",
                   reply_markup=markup([[Button("Отменить подарок", f"gift:decline:{g.id}")]]))


@router.callback_query(F.data.startswith("gift:"))
async def gift_action(c: CallbackQuery, user: User, app: App):
    _, action, gid = c.data.split(":")
    async with app.tx() as ctx:
        u = await users.get(ctx, user.id)
        if action == "accept":
            g = await gifts.accept(ctx, u, int(gid))
            reply = f"Вы получили {g.shares} SBAID."
        else:
            await gifts.decline(ctx, u, int(gid))
            reply = "Подарок отменён."
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(reply)
    await c.answer()


@router.message(Command("portfolio"))
async def portfolio(m: Message, user: User, app: App):
    async with app.tx() as ctx:
        u = await users.get(ctx, user.id)
        h = await registry.holding(ctx, u.id)
        share = await registry.voting_share(ctx, u.id)
        points = await users.penalty_points(ctx, u.id)
        ords = await orders.active_orders(ctx, u.id)
        trs = await trades.user_trades(ctx, u.id, limit=10)
    lines = [f"SBAID: {h.total} (заблокировано {h.blocked})", f"Доля голосов: {share * 100:.4f}%",
             f"Завершённых сделок: {u.completed_trades}", f"Штрафных баллов за полгода: {points}"]
    if u.banned_until and u.banned_until > app.clock():
        lines.append(f"Торговля запрещена до {fmt_msk(u.banned_until)}")
    if ords:
        lines.append("\nАктивные заявки:")
        lines += [f"• №{o.id} {SIDE[o.side]} {o.remaining} по {rub(o.limit_price)}" for o in ords]
    if trs:
        lines.append("\nПоследние сделки:")
        lines += [f"• №{t.id} {'покупка' if t.buyer_id == u.id else 'продажа'} {t.shares} по {rub(t.price)} — "
                  f"{TRADE_STATUS[t.status]}" for t in trs]
    await m.answer("\n".join(lines))


@router.message(Command("exit"))
async def exit_start(m: Message, user: User, app: App, state: FSMContext):
    async with app.tx() as ctx:
        u = await users.get(ctx, user.id)
        users.require_member(u)
        o = await exits.options(ctx, u)
    market = ("Выйти из клуба по рыночной цене можно в любой момент: подайте заявку на продажу (/sell), "
              "и на ближайшем торговом дне ваши SBAID купят другие участники. Деньги вам переведёт покупатель.")
    if o["redeemable"] == 0:
        return await m.answer(market)
    await state.set_state(ExitFlow.qty)
    await m.answer(f"{market}\n\nЕсли вы отказываетесь от участия и хотите вернуть основателю SBAID, купленные на "
                   f"размещении, можно подать заявление на возврат — до {o['redeemable']} SBAID. Основатель "
                   f"вернёт не больше уплаченного за вычетом фактических расходов. Погашенные SBAID "
                   f"возвращаются в казну и перестают голосовать.\n\nСколько SBAID вернуть? "
                   f"Чтобы отказаться, отправьте /start.")


@router.message(ExitFlow.qty, F.text, ~F.text.startswith("/"))
async def exit_qty(m: Message, user: User, app: App, state: FSMContext):
    qty = parse_int(m.text)
    async with app.tx() as ctx:
        await exits.request(ctx, await users.get(ctx, user.id), qty)
    await state.clear()


@router.message(Command("linked"))
async def linked(m: Message, app: App):
    async with app.tx() as ctx:
        rows = await admin.linked_list(ctx)
    await m.answer("Аккаунты, связанные с основателем (не торгуют на рынке между участниками):\n"
                   + ("\n".join(f"• {u.display}" for u in rows) or "пока нет"))


@router.message(Command("registry"))
async def public_registry(m: Message, user: User, app: App):
    async with app.tx() as ctx:
        data = await admin.export_registry(ctx, user, public=True)
    await m.answer_document(BufferedInputFile(data.encode("utf-8-sig"), "registry_public.csv"))


# --- админ ---------------------------------------------------------------------------
ADMIN_HELP = ("Админ-команды:\n"
              "/newauction <объём> <дней> [мин. цена ₽] — размещение аукционом: цену задаёт спрос\n"
              "/newplacement <объём> <дней> [цена ₽] — размещение по цене (без цены — рыночная)\n"
              "/closeplacement — закрыть текущее размещение\n"
              "/testpay <№ счёта> — пробный режим: подтвердить оплату вручную, пока нет Robokassa\n"
              "/disputes — споры; /resolve <№> transfer|cancel <комментарий>\n"
              "/refunds — заявки на выход; /refund <№> <сумма ₽ | reject> [комментарий]; /refund_done <№>\n"
              "/link @ник, /unlink @ник — связанные аккаунты\n"
              "/observer @ник, /unobserver @ник — наблюдатели\n"
              "/settings; /set <ключ> <значение>\n"
              "/tradingday — провести сорвавшийся торговый день\n"
              "/export registry|journal|public\n"
              "/broadcast <текст>\n"
              "/adjust @ник <±SBAID> <причина>\n"
              "/penalty @ник <баллы> <причина>\n"
              "/journal — последние записи аудита")


def args(command: CommandObject, n: int, usage: str) -> list[str]:
    parts = (command.args or "").split(maxsplit=n - 1)
    if len(parts) < n:
        raise DomainError(f"Формат: {usage}")
    return parts


@router.message(Command("admin"))
async def admin_help(m: Message, user: User):
    users.require_staff(user)
    await m.answer(ADMIN_HELP)


@router.message(Command("newplacement", "newauction"))
async def new_placement(m: Message, command: CommandObject, user: User, app: App):
    auction = command.command == "newauction"
    parts = (command.args or "").split()
    if len(parts) < 2:
        raise DomainError("Формат: /newauction <объём> <дней> [мин. цена ₽]" if auction
                          else "Формат: /newplacement <объём> <дней> [цена ₽]")
    volume, days = parse_int(parts[0]), parse_int(parts[1])
    price = parse_rub(parts[2]) if len(parts) > 2 else None
    async with app.tx() as ctx:
        p = await placements.create(ctx, await users.get(ctx, user.id), volume, price, days,
                                    mode="auction" if auction else "fixed")
    steps = "\n".join(f"• {s}" for s in p.price_calc["steps"])
    await m.answer(f"Черновик размещения №{p.number}: {p.volume} SBAID, лот {p.lot}, "
                   f"лимит в одни руки {p.per_user_limit}, до {fmt_msk(p.ends_at)}.\n{placements.ladder_text(p)}\n\n"
                   f"Стартовая цена:\n{steps}",
                   reply_markup=markup([[Button("Опубликовать", f"pl:publish:{p.id}")]]))


@router.callback_query(F.data.startswith("pl:publish:"))
async def publish_placement(c: CallbackQuery, user: User, app: App):
    async with app.tx() as ctx:
        p = await placements.publish(ctx, await users.get(ctx, user.id), int(c.data.split(":")[2]))
    await c.message.edit_text(f"Размещение №{p.number} опубликовано в канале.")
    await c.answer()


@router.message(Command("testpay"))
async def testpay(m: Message, command: CommandObject, user: User, app: App):
    inv = parse_int(args(command, 1, "/testpay <№ счёта>")[0])
    async with app.tx() as ctx:
        status = await placements.test_payment(ctx, await users.get(ctx, user.id), inv)
    await m.answer({"credited": f"Счёт №{inv} отмечен оплаченным, SBAID начислены.",
                    "duplicate": f"Счёт №{inv} уже был оплачен."}.get(status, f"Счёт №{inv}: {status}"))


@router.message(Command("closeplacement"))
async def close_placement(m: Message, user: User, app: App):
    async with app.tx() as ctx:
        p = await placements.current(ctx)
        if p is None:
            raise DomainError("Нет открытого размещения")
        p = await placements.close(ctx, await users.get(ctx, user.id), p.id)
    await m.answer(f"Размещение №{p.number} закрыто: продано {p.sold} из {p.volume}.")


@router.message(Command("disputes"))
async def disputes(m: Message, user: User, app: App, bot: Bot):
    users.require_staff(user)
    async with app.tx() as ctx:
        rows = await trades.open_disputes(ctx)
        items = []
        for d in rows:
            t = await ctx.s.get(Trade, d.trade_id)
            b, s = await users.get(ctx, t.buyer_id), await users.get(ctx, t.seller_id)
            items.append((d, t, b, s))
    if not items:
        return await m.answer("Открытых споров нет.")
    for d, t, b, s in items:
        await m.answer(f"Спор №{d.id} по сделке №{t.id}: {t.shares} SBAID, {rub(t.amount)}\n"
                       f"Покупатель: {b.display} ({b.full_name})\nПродавец: {s.display} ({s.full_name})\n"
                       f"Причина: {d.reason}\nРешить до {fmt_msk(d.deadline)}\n"
                       f"Доказательств: {len(d.evidence)}\n\n"
                       f"/resolve {d.id} transfer <комментарий> — перенести SBAID\n"
                       f"/resolve {d.id} cancel <комментарий> — отменить сделку")
        for e in d.evidence:
            who = "покупателя" if e["user_id"] == t.buyer_id else "продавца"
            send = bot.send_photo if e["kind"] == "photo" else bot.send_document
            await send(m.chat.id, e["file_id"], caption=f"Спор №{d.id}: доказательство {who}")


@router.message(Command("resolve"))
async def resolve(m: Message, command: CommandObject, user: User, app: App):
    did, decision, comment = args(command, 3, "/resolve <№> transfer|cancel <комментарий>")
    async with app.tx() as ctx:
        d = await trades.resolve_dispute(ctx, await users.get(ctx, user.id), parse_int(did), decision, comment)
    await m.answer(f"Спор №{d.id} решён.")


@router.message(Command("refunds"))
async def refunds(m: Message, user: User, app: App):
    users.require_founder(user)
    async with app.tx() as ctx:
        rows = await exits.pending(ctx)
        lines = []
        for r in rows:
            u = await users.get(ctx, r.user_id)
            lines.append(f"№{r.id}: {u.display}, {r.shares} SBAID, уплачено {rub(r.max_amount)}, статус {r.status}"
                         + (f", к возврату {rub(r.amount)}" if r.amount is not None else ""))
    await m.answer("\n".join(lines) or "Заявок нет.")


@router.message(Command("refund"))
async def refund(m: Message, command: CommandObject, user: User, app: App):
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 2:
        raise DomainError("Формат: /refund <№> <сумма ₽ | reject> [комментарий]")
    comment = parts[2] if len(parts) > 2 else ""
    if parts[1] == "reject":
        amount = None
    elif parts[1] in ("0", "0.0"):
        amount = 0
    else:
        amount = parse_rub(parts[1])
    async with app.tx() as ctx:
        r = await exits.decide(ctx, await users.get(ctx, user.id), parse_int(parts[0]), amount, comment)
    await m.answer(f"Заявка №{r.id}: {r.status}.")


@router.message(Command("refund_done"))
async def refund_done(m: Message, command: CommandObject, user: User, app: App):
    parts = args(command, 1, "/refund_done <№> [номер возврата]")[0].split(maxsplit=1)
    async with app.tx() as ctx:
        r = await exits.mark_done(ctx, await users.get(ctx, user.id), parse_int(parts[0]),
                                  parts[1] if len(parts) > 1 else "")
    await m.answer(f"Возврат №{r.id} отмечен проведённым.")


@router.message(Command("link", "unlink"))
async def link(m: Message, command: CommandObject, user: User, app: App):
    nick = args(command, 1, f"/{command.command} @ник")[0]
    async with app.tx() as ctx:
        u = await admin.set_linked(ctx, await users.get(ctx, user.id), nick, command.command == "link")
    await m.answer(f"{u.display}: {'связан с основателем' if u.linked_to_founder else 'пометка снята'}.")


@router.message(Command("observer", "unobserver"))
async def observer(m: Message, command: CommandObject, user: User, app: App):
    nick = args(command, 1, f"/{command.command} @ник")[0]
    async with app.tx() as ctx:
        u = await admin.set_observer(ctx, await users.get(ctx, user.id), nick, command.command == "observer")
    await m.answer(f"{u.display}: роль {'наблюдатель' if u.role == Role.OBSERVER else 'участник'}.")


@router.message(Command("settings"))
async def show_settings(m: Message, user: User, app: App):
    users.require_staff(user)
    async with app.tx() as ctx:
        lines = [f"{k} = {await settings.get(ctx, k)} — {desc}" for k, (_, desc) in settings.DEFAULTS.items()]
        pend = await settings.pending_changes(ctx)
    if pend:
        lines.append("\nЖдут вступления в силу:")
        lines += [f"• {p.key} → {p.value['v']} с {fmt_msk(p.effective_at)}" for p in pend]
    await m.answer("\n".join(lines))


@router.message(Command("set"))
async def set_setting(m: Message, command: CommandObject, user: User, app: App):
    key, value = args(command, 2, "/set <ключ> <значение>")
    users.require_founder(user)
    async with app.tx() as ctx:
        sc = await settings.change(ctx, user.id, key, parse_int(value))
    await m.answer(f"Изменение опубликовано в канале и вступит в силу {fmt_msk(sc.effective_at)}.")


@router.message(Command("tradingday"))
async def trading_day(m: Message, user: User, app: App):
    async with app.tx() as ctx:
        cutoff = await trading.last_cutoff(ctx)
        sess = await trading.run(ctx, cutoff, actor=await users.get(ctx, user.id))
    await m.answer(f"Торговый день {fmt_msk(cutoff)}: " + (
        f"цена {rub(sess.price)}, объём {sess.volume}, сделок {sess.trades_count}." if sess.status == "done"
        else "не состоялся."))


@router.message(Command("export"))
async def export(m: Message, command: CommandObject, user: User, app: App):
    kind = args(command, 1, "/export registry|journal|public")[0]
    async with app.tx() as ctx:
        u = await users.get(ctx, user.id)
        if kind == "journal":
            data = await admin.export_journal(ctx, u)
        elif kind in ("registry", "public"):
            data = await admin.export_registry(ctx, u, public=kind == "public")
        else:
            raise DomainError("Формат: /export registry|journal|public")
    await m.answer_document(BufferedInputFile(data.encode("utf-8-sig"), f"{kind}.csv"))


@router.message(Command("broadcast"))
async def broadcast(m: Message, command: CommandObject, user: User, app: App):
    text = args(command, 1, "/broadcast <текст>")[0]
    async with app.tx() as ctx:
        n = await admin.broadcast(ctx, await users.get(ctx, user.id), text)
    await m.answer(f"Рассылка отправлена, получателей: {n}.")


@router.message(Command("adjust"))
async def adjust(m: Message, command: CommandObject, user: User, app: App):
    nick, delta, reason = args(command, 3, "/adjust @ник <±SBAID> <причина>")
    async with app.tx() as ctx:
        await admin.adjust(ctx, await users.get(ctx, user.id), nick, parse_int(delta), reason)
    await m.answer("Корректировка записана в журнал, владелец уведомлён.")


@router.message(Command("penalty"))
async def penalty(m: Message, command: CommandObject, user: User, app: App):
    nick, points, reason = args(command, 3, "/penalty @ник <баллы> <причина>")
    async with app.tx() as ctx:
        await admin.manual_penalty(ctx, await users.get(ctx, user.id), nick, parse_int(points), reason)
    await m.answer("Штрафные баллы начислены.")


@router.message(Command("journal"))
async def journal(m: Message, user: User, app: App):
    async with app.tx() as ctx:
        rows = await admin.audit_tail(ctx, await users.get(ctx, user.id))
    await m.answer("\n".join(f"{fmt_msk(r.created_at)} · {r.actor_id or 'бот'} · {r.action} · {r.data}"[:300]
                             for r in rows)[:4000] or "Журнал пуст.")


@router.message(Command("help"))
async def help_(m: Message, user: User):
    await m.answer(HELP + ("\n\n/admin — команды администратора" if user.role in users.STAFF_ROLES else ""))


@router.message(F.text)
async def fallback(m: Message):
    await m.answer("Не понял. " + HELP)


# --- служебное ------------------------------------------------------------------------
service = Router()


@service.my_chat_member()
async def bot_added(event: ChatMemberUpdated, app: App, bot: Bot):
    """Основатель добавляет бота админом в канал или группу — бот запоминает этот
    чат как открытый канал или закрытую группу. Чужие добавления игнорируются."""
    founder_id = app.config.founder_telegram_id
    if event.chat.type == "private" or not founder_id:
        return
    if event.from_user.id != founder_id or event.new_chat_member.status != "administrator":
        return
    kind = "channel" if event.chat.type == "channel" else "group"
    await app.notifier.bind_chat(kind, event.chat.id)
    async with app.tx() as ctx:
        from ..core.audit import audit
        audit(ctx, None, "chat.bind", kind=kind, chat_id=event.chat.id, title=event.chat.title)
    what = "открытый канал для итогов и объявлений" if kind == "channel" else "закрытая группа холдеров"
    try:
        await bot.send_message(founder_id, f"✅ «{event.chat.title}» подключён как {what}.")
    except Exception:  # noqa: BLE001 — основатель ещё не нажал /start
        log.info("Привязан %s «%s» (%s)", kind, event.chat.title, event.chat.id)


def build_dispatcher(app: App) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(UserMiddleware(app))

    @dp.errors(ExceptionTypeFilter(DomainError))
    async def domain_error(event: ErrorEvent):
        text = f"⚠️ {event.exception}"
        upd = event.update
        if upd.callback_query:
            await upd.callback_query.answer(text[:200], show_alert=True)
        elif upd.message:
            await upd.message.answer(text)
        return True

    dp.include_router(service)
    dp.include_router(router)
    return dp

