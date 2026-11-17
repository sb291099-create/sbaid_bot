"""Схема базы данных (PostgreSQL).

Акции — целые числа, деньги — в копейках, время — UTC.
Баланс участника всегда равен сумме его записей в журнале transfers;
holdings — материализованный остаток, который меняется только вместе с
записью в журнал (см. core/registry.py).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _ts(nullable: bool = True):
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class Role:
    GUEST = "guest"
    MEMBER = "member"
    FOUNDER = "founder"
    OBSERVER = "observer"
    TREASURY = "treasury"  # служебный «пользователь» — казна клуба


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    contact: Mapped[str | None] = mapped_column(String(255))  # телефон или e-mail
    offer_accepted_at: Mapped[datetime | None] = _ts()
    offer_version: Mapped[str | None] = mapped_column(String(32))
    pd_consent_at: Mapped[datetime | None] = _ts()
    # подтверждение совершеннолетия (18+), даётся вместе с принятием оферты
    adult_confirmed_at: Mapped[datetime | None] = _ts()
    role: Mapped[str] = mapped_column(String(16), default=Role.GUEST)
    linked_to_founder: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_trades: Mapped[int] = mapped_column(Integer, default=0)
    # Реквизиты для приёма оплаты (СБП-телефон и банк), зашифрованы.
    payout_details_enc: Mapped[str | None] = mapped_column(Text)
    group_invite_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def display(self) -> str:
        return f"@{self.username}" if self.username else f"id{self.id}"


class Holding(Base):
    __tablename__ = "holdings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        CheckConstraint("total >= 0", name="holdings_total_nonneg"),
        CheckConstraint("blocked >= 0 AND blocked <= total", name="holdings_blocked_range"),
    )


class Placement(Base):
    __tablename__ = "placements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True)
    volume: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(BigInteger)  # стартовая цена, копейки за SBAID
    # лесенка: цена растёт на ladder_step после каждых ladder_every проданных (0 — одна цена)
    # fixed — продажа по цене (с лесенкой или без); auction — аукцион заявок, как при IPO
    mode: Mapped[str] = mapped_column(String(8), default="fixed")
    # для аукциона: минимальная цена заявки (0 — без минимума); после итогов price = цена отсечения
    min_price: Mapped[int] = mapped_column(BigInteger, default=0)
    ladder_every: Mapped[int] = mapped_column(Integer, default=0)
    ladder_step: Mapped[int] = mapped_column(BigInteger, default=0)
    starts_at: Mapped[datetime] = _ts(False)
    ends_at: Mapped[datetime] = _ts(False)
    lot: Mapped[int] = mapped_column(Integer)
    per_user_limit: Mapped[int] = mapped_column(Integer)
    sold: Mapped[int] = mapped_column(Integer, default=0)
    # draft → open → closed
    status: Mapped[str] = mapped_column(String(16), default="draft")
    price_calc: Mapped[dict | None] = mapped_column(JSONB)


class PlacementBid(Base):
    """Заявка на аукционе размещения: «куплю shares SBAID по цене до max_price»."""
    __tablename__ = "placement_bids"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    placement_id: Mapped[int] = mapped_column(ForeignKey("placements.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    shares: Mapped[int] = mapped_column(Integer)
    max_price: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts(False)
    # active | cancelled | won | partial | lost
    status: Mapped[str] = mapped_column(String(16), default="active")
    allocated: Mapped[int] = mapped_column(Integer, default=0)


class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # = InvId в Robokassa
    placement_id: Mapped[int] = mapped_column(ForeignKey("placements.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    shares: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(BigInteger)  # копейки
    # reserved → paid | expired | paid_late (оплата после снятия резерва без остатка)
    status: Mapped[str] = mapped_column(String(16), default="reserved")
    reserved_until: Mapped[datetime] = _ts(False)
    paid_at: Mapped[datetime | None] = _ts()
    robokassa_op: Mapped[str | None] = mapped_column(String(128))
    receipt_number: Mapped[str | None] = mapped_column(String(128))
    # Сколько акций этой покупки ещё у участника (не проданы, не подарены,
    # не погашены). Нужно, чтобы вернуть деньги только за акции с размещения.
    shares_remaining: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    shares: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts(False)
    expires_at: Mapped[datetime] = _ts(False)
    filled: Mapped[int] = mapped_column(Integer, default=0)
    # active | partial | filled | cancelled | expired
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)

    @property
    def remaining(self) -> int:
        return self.shares - self.filled


class TradeStatus:
    AWAITING_PAYMENT = "awaiting_payment"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    OPEN = (AWAITING_PAYMENT, AWAITING_CONFIRMATION, DISPUTED)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    buy_order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    sell_order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    shares: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[int] = mapped_column(BigInteger)
    pay_deadline: Mapped[datetime] = _ts(False)
    confirm_deadline: Mapped[datetime] = _ts(False)
    buyer_paid_at: Mapped[datetime | None] = _ts()
    seller_confirmed_at: Mapped[datetime | None] = _ts()
    status: Mapped[str] = mapped_column(String(24), default=TradeStatus.AWAITING_PAYMENT, index=True)
    reminders_sent: Mapped[list] = mapped_column(JSONB, default=list)
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    completed_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(False)


class Transfer(Base):
    """Неизменяемый журнал движения акций. Записи не удаляются и не правятся
    (это обеспечивает триггер, см. db.py)."""
    __tablename__ = "transfers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    to_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    shares: Mapped[int] = mapped_column(Integer)
    # issue | placement | trade | gift | exit | adjustment
    type: Mapped[str] = mapped_column(String(16))
    basis: Mapped[str] = mapped_column(Text)  # основание: «trade:12», «purchase:5» …
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("shares > 0", name="transfers_positive"),)


class Gift(Base):
    __tablename__ = "gifts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    shares: Mapped[int] = mapped_column(Integer)
    # pending | accepted | declined | cancelled | rejected_limit
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id"))
    shares: Mapped[int] = mapped_column(Integer)
    max_amount: Mapped[int] = mapped_column(BigInteger)  # уплачено за эти акции
    amount: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    # auto — в пределах 14 дней; admin — по решению админа
    kind: Mapped[str] = mapped_column(String(8))
    # pending_admin | processing | done | rejected | failed
    status: Mapped[str] = mapped_column(String(16))
    robokassa_refund_id: Mapped[str | None] = mapped_column(String(128))
    receipt_number: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("amount IS NULL OR amount <= max_amount", name="refund_not_more_than_paid"),
    )


class Dispute(Base):
    __tablename__ = "disputes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), unique=True)
    opened_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # None — открыт ботом по сроку
    reason: Mapped[str] = mapped_column(Text)
    # [{"user_id": .., "file_id": .., "kind": "photo|document", "at": ..}]
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    decision: Mapped[str | None] = mapped_column(String(16))  # transfer | cancel
    comment: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = _ts()
    deadline: Mapped[datetime] = _ts(False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PriceHistory(Base):
    __tablename__ = "price_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[datetime] = _ts(False)
    source: Mapped[str] = mapped_column(String(16))  # trade | placement
    price: Mapped[int] = mapped_column(BigInteger)
    volume: Mapped[int] = mapped_column(Integer)


class SettingChange(Base):
    """История настроек: значение действует с effective_at (не раньше 7 дней
    после публикации в канале)."""
    __tablename__ = "setting_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[dict] = mapped_column(JSONB)
    published_at: Mapped[datetime] = _ts(False)
    effective_at: Mapped[datetime] = _ts(False)
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class AppState(Base):
    """Служебные значения, которые бот узнаёт сам (например, id канала и группы)."""
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
