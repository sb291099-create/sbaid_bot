"""Контекст операции ядра: транзакция БД, часы, настройки и «исходящие».

Ядро не знает про Telegram. Все сообщения пользователям складываются в
ctx.outbox и отправляются через Notifier только после успешного коммита —
так пользователь не получит «акции зачислены», если транзакция откатилась.
Чтобы подключить MAX, достаточно написать ещё одну реализацию Notifier.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Config
from ..crypto import Cipher
from ..timeutil import utcnow


@dataclass
class Button:
    text: str
    data: str | None = None  # callback data
    url: str | None = None


Keyboard = list[list[Button]]


class Notifier(Protocol):
    async def send(self, user_id: int, text: str, buttons: Keyboard | None = None) -> None: ...
    async def notify_staff(self, text: str, buttons: Keyboard | None = None) -> None: ...
    async def publish(self, text: str) -> None: ...
    async def invite_to_group(self, user_id: int) -> None: ...


class NullNotifier:
    """Notifier для тестов: запоминает все сообщения."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any, str]] = []

    async def send(self, user_id, text, buttons=None):
        self.sent.append(("user", user_id, text))

    async def notify_staff(self, text, buttons=None):
        self.sent.append(("staff", None, text))

    async def publish(self, text):
        self.sent.append(("channel", None, text))

    async def invite_to_group(self, user_id):
        self.sent.append(("invite", user_id, ""))

    def to(self, user_id: int) -> list[str]:
        return [t for kind, uid, t in self.sent if kind == "user" and uid == user_id]


@dataclass
class Ctx:
    s: AsyncSession
    app: "App"
    now: datetime
    outbox: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    @property
    def config(self) -> Config:
        return self.app.config

    @property
    def cipher(self) -> Cipher:
        return self.app.cipher

    def send(self, user_id: int, text: str, buttons: Keyboard | None = None) -> None:
        self.outbox.append(lambda: self.app.notifier.send(user_id, text, buttons))

    def staff(self, text: str, buttons: Keyboard | None = None) -> None:
        self.outbox.append(lambda: self.app.notifier.notify_staff(text, buttons))

    def publish(self, text: str) -> None:
        self.outbox.append(lambda: self.app.notifier.publish(text))

    def invite(self, user_id: int) -> None:
        self.outbox.append(lambda: self.app.notifier.invite_to_group(user_id))


class App:
    """Точка входа в ядро: всё, что нужно сервисам, кроме мессенджера."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], config: Config,
                 notifier: Notifier, cipher: Cipher, payments: Any = None,
                 clock: Callable[[], datetime] = utcnow):
        self.sm = sessionmaker
        self.config = config
        self.notifier = notifier
        self.cipher = cipher
        self.payments = payments
        self.clock = clock

    @asynccontextmanager
    async def tx(self):
        async with self.sm() as s:
            async with s.begin():
                ctx = Ctx(s=s, app=self, now=self.clock())
                yield ctx
            for job in ctx.outbox:
                try:
                    await job()
                except Exception:  # noqa: BLE001 — сбой доставки не должен ронять операцию
                    import logging
                    logging.getLogger(__name__).exception("Не удалось отправить уведомление")
