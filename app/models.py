from sqlalchemy import (
    BigInteger, Integer, String, Text, ForeignKey, DateTime, Enum, Boolean
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from enum import Enum as PyEnum
from app.db import Base

class LotStatus(str, PyEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    FINISHED = "finished"

class OfferStatus(str, PyEnum):
    OFFERED = "offered"
    POSTPONED = "postponed"
    PAID = "paid"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELED = "canceled"  # коли товар закінчився, решту інвалідуємо

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(64))
    last_name: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Lot(Base):
    __tablename__ = "lots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    public_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    # Весь опис зберігаємо в title (за новими вимогами)
    title: Mapped[str] = mapped_column(String(200))
    # поля залишаємо, але не використовуємо в новому майстрі
    condition: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[str] = mapped_column(String(200), default="")
    start_price: Mapped[int] = mapped_column(Integer)
    # Якщо min_step == 0 → це режим Розпродаж (SALE, кнопка «Купити»)
    min_step: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(Enum(LotStatus), default=LotStatus.DRAFT)
    current_price: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_message_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    current_winner_tg_id: Mapped[int | None] = mapped_column(BigInteger)

    photos: Mapped[list["LotPhoto"]] = relationship(back_populates="lot", cascade="all, delete-orphan")
    bids: Mapped[list["Bid"]] = relationship(back_populates="lot", cascade="all, delete-orphan")
    offers: Mapped[list["Offer"]] = relationship(back_populates="lot", cascade="all, delete-orphan")

class LotPhoto(Base):
    __tablename__ = "lot_photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"))
    file_id: Mapped[str] = mapped_column(String(200))
    lot: Mapped[Lot] = relationship(back_populates="photos")

class Bid(Base):
    __tablename__ = "bids"
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    lot: Mapped[Lot] = relationship(back_populates="bids")

class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    offered_price: Mapped[int] = mapped_column(Integer)
    rank_index: Mapped[int] = mapped_column(Integer)  # 1=топ-претендент
    status: Mapped[str] = mapped_column(Enum(OfferStatus), default=OfferStatus.OFFERED, index=True)
    hold_until: Mapped[datetime | None] = mapped_column(DateTime)
    invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    invoice_url: Mapped[str | None] = mapped_column(String(256))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)   
    contact_fullname: Mapped[str | None] = mapped_column(String(200))
    contact_phone: Mapped[str | None] = mapped_column(String(64))
    contact_city_region: Mapped[str | None] = mapped_column(String(200))
    contact_delivery: Mapped[str | None] = mapped_column(String(64))
    contact_address: Mapped[str | None] = mapped_column(String(200))
    contact_comment: Mapped[str | None] = mapped_column(Text)
    # нагадування за 5 годин до дедлайну
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    lot: Mapped[Lot] = relationship(back_populates="offers")
