import os
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy import text

from torgi.config import Config
from torgi.core.context import App, NullNotifier
from torgi.crypto import Cipher
from torgi.db import init_db, make_engine, make_sessionmaker
from torgi.models import Base, Role
from torgi.payments.robokassa import Robokassa

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://torgi:torgi@localhost/torgi_test")


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now


@pytest_asyncio.fixture
async def app():
    engine = make_engine(TEST_DB)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(founder_telegram_id=1, total_shares=1_000_000, encryption_key=Cipher.generate_key())
    await init_db(engine, cfg.total_shares)
    # понедельник, 10:00 МСК
    clock = Clock(datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc))
    rk = Robokassa("shop", "p1", "p2", "p3", test_mode=True)
    a = App(make_sessionmaker(engine), cfg, NullNotifier(), Cipher(cfg.encryption_key), rk, clock=clock)
    a.clock_obj = clock
    yield a
    await engine.dispose()


__all__ = ["Base", "Role"]
