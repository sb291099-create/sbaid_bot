"""Подключение к базе, создание схемы и стартовый выпуск акций в казну."""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base, Holding, Role, Transfer, User

IMMUTABLE_SQL = [
    """
    CREATE OR REPLACE FUNCTION torgi_forbid_change() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'таблица % только для добавления записей', TG_TABLE_NAME;
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS transfers_immutable ON transfers;",
    """CREATE TRIGGER transfers_immutable BEFORE UPDATE OR DELETE ON transfers
       FOR EACH ROW EXECUTE FUNCTION torgi_forbid_change();""",
    "DROP TRIGGER IF EXISTS audit_immutable ON audit_log;",
    """CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_log
       FOR EACH ROW EXECUTE FUNCTION torgi_forbid_change();""",
]


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine, total_shares: int) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in IMMUTABLE_SQL:
            await conn.execute(text(stmt))
    sm = make_sessionmaker(engine)
    async with sm() as s, s.begin():
        treasury = (await s.execute(select(User).where(User.role == Role.TREASURY))).scalar_one_or_none()
        if treasury is None:
            treasury = User(role=Role.TREASURY, username=None, full_name="Казна клуба")
            s.add(treasury)
            await s.flush()
            s.add(Holding(user_id=treasury.id, total=total_shares, blocked=0))
            s.add(Transfer(from_user_id=None, to_user_id=treasury.id, shares=total_shares,
                           type="issue", basis="Выпуск SBAID"))
