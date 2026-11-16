"""Конфигурация из переменных окружения.

Секреты (токен бота, пароли Robokassa, ключ шифрования) берутся только из
окружения и никогда не хранятся в коде или базе.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value else None


@dataclass(frozen=True)
class Config:
    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://torgi:torgi@localhost/torgi"
    # Telegram id основателя (админа). Он же получает уведомления о спорах.
    founder_telegram_id: int | None = None
    # Закрытая группа акционеров и открытый канал с итогами.
    shareholders_group_id: int | None = None
    public_channel_id: int | None = None
    # Всего акций в клубе (казна + участники).
    total_shares: int = 1_000_000
    # Ключ Fernet для шифрования реквизитов продавцов.
    encryption_key: str = ""
    # Ссылки на юридические тексты.
    offer_url: str = ""
    offer_version: str = "1.0"
    rules_url: str = ""
    privacy_url: str = ""
    # Robokassa
    robokassa_login: str = ""
    robokassa_password1: str = ""
    robokassa_password2: str = ""
    robokassa_password3: str = ""
    robokassa_test_mode: bool = True
    # usn_income — ИП на УСН «доходы»; npd — самозанятый (Робочеки СМЗ).
    robokassa_sno: str = "usn_income"
    receipt_item_name: str = "Участие в клубе, {n} акций (голосов)"
    # HTTP-сервер для уведомлений Robokassa (ResultURL).
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    timezone: str = "Europe/Moscow"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(
            bot_token=e("BOT_TOKEN", ""),
            database_url=e("DATABASE_URL", cls.database_url),
            founder_telegram_id=_int_or_none(e("FOUNDER_TELEGRAM_ID")),
            shareholders_group_id=_int_or_none(e("SHAREHOLDERS_GROUP_ID")),
            public_channel_id=_int_or_none(e("PUBLIC_CHANNEL_ID")),
            total_shares=int(e("TOTAL_SHARES", "1000000")),
            encryption_key=e("ENCRYPTION_KEY", ""),
            offer_url=e("OFFER_URL", ""),
            offer_version=e("OFFER_VERSION", "1.0"),
            rules_url=e("RULES_URL", ""),
            privacy_url=e("PRIVACY_URL", ""),
            robokassa_login=e("ROBOKASSA_LOGIN", ""),
            robokassa_password1=e("ROBOKASSA_PASSWORD1", ""),
            robokassa_password2=e("ROBOKASSA_PASSWORD2", ""),
            robokassa_password3=e("ROBOKASSA_PASSWORD3", ""),
            robokassa_test_mode=e("ROBOKASSA_TEST_MODE", "1") == "1",
            robokassa_sno=e("ROBOKASSA_SNO", "usn_income"),
            receipt_item_name=e("RECEIPT_ITEM_NAME", cls.receipt_item_name),
            web_host=e("WEB_HOST", "0.0.0.0"),
            web_port=int(e("WEB_PORT", "8080")),
        )
