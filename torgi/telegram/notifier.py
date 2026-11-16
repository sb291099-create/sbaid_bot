"""Реализация Notifier для Telegram. Аналогичный класс можно написать для MAX."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Config
from ..core.context import Keyboard
from ..models import AppState, Role, User

log = logging.getLogger(__name__)


def markup(buttons: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b.text, callback_data=b.data, url=b.url) for b in row] for row in buttons])


class TelegramNotifier:
    def __init__(self, bot: Bot, sessionmaker: async_sessionmaker, config: Config):
        self.bot, self.sm, self.config = bot, sessionmaker, config

    async def chat_id(self, kind: str) -> int | None:
        """id канала ("channel") или группы ("group"): из .env, а если там пусто —
        тот, что бот запомнил, когда основатель добавил его админом."""
        fixed = self.config.public_channel_id if kind == "channel" else self.config.shareholders_group_id
        if fixed:
            return fixed
        async with self.sm() as s:
            row = await s.get(AppState, f"{kind}_id")
            return int(row.value) if row else None

    async def bind_chat(self, kind: str, chat_id: int) -> None:
        async with self.sm() as s, s.begin():
            row = await s.get(AppState, f"{kind}_id")
            if row:
                row.value = str(chat_id)
            else:
                s.add(AppState(key=f"{kind}_id", value=str(chat_id)))

    async def _tg_id(self, user_id: int) -> int | None:
        async with self.sm() as s:
            u = await s.get(User, user_id)
            return u.telegram_id if u else None

    async def send(self, user_id: int, text: str, buttons: Keyboard | None = None) -> None:
        tg_id = await self._tg_id(user_id)
        if tg_id:
            await self.bot.send_message(tg_id, text, reply_markup=markup(buttons))

    async def notify_staff(self, text: str, buttons: Keyboard | None = None) -> None:
        async with self.sm() as s:
            ids = (await s.execute(select(User.telegram_id).where(
                User.role.in_((Role.FOUNDER, Role.OBSERVER)), User.telegram_id.is_not(None)))).scalars().all()
        for tg_id in ids:
            try:
                await self.bot.send_message(tg_id, text, reply_markup=markup(buttons))
            except Exception:  # noqa: BLE001
                log.exception("Не удалось написать сотруднику %s", tg_id)

    async def publish(self, text: str) -> None:
        channel = await self.chat_id("channel")
        if not channel:
            log.warning("Канал не привязан, публикация пропущена: %s", text)
            return
        await self.bot.send_message(channel, text)

    async def invite_to_group(self, user_id: int) -> None:
        """Одноразовая ссылка в закрытую группу акционеров."""
        tg_id = await self._tg_id(user_id)
        group = await self.chat_id("group")
        if not tg_id or not group:
            return
        link = await self.bot.create_chat_invite_link(group, member_limit=1, name=f"user {user_id}")
        await self.bot.send_message(
            tg_id, f"🎉 Теперь у вас есть SBAID, вы в клубе! Ваша личная одноразовая ссылка в закрытую группу "
                   f"«{self.config.group_name}»:\n{link.invite_link}")
