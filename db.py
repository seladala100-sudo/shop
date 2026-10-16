from datetime import datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric,
                        String, Text, select)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(256), default="")
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    spent: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    kind: Mapped[str] = mapped_column(String(10), default="manual")  # manual | vpn
    vpn_days: Mapped[int] = mapped_column(Integer, default=30)
    vpn_gb: Mapped[int] = mapped_column(Integer, default=0)  # 0 = безлимит
    input_prompt: Mapped[str | None] = mapped_column(String(200))  # что спросить у покупателя
    stock: Mapped[int | None] = mapped_column(Integer)  # None = без лимита
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    product_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(150))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    input_data: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(12), default="processing")  # processing|done|refunded
    result: Mapped[str] = mapped_column(Text, default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    invoice_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending|paid|expired
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class VpnKey(Base):
    __tablename__ = "vpn_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[int] = mapped_column(Integer)
    sub_url: Mapped[str] = mapped_column(Text)
    expire: Mapped[datetime | None] = mapped_column(DateTime)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[int] = mapped_column(Integer, unique=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as s:
        if await s.scalar(select(Category.id).limit(1)) is None:
            cats = ["⭐ Telegram Stars", "🎁 Подарки и NFT", "🗑 Удалённые подарки",
                    "💎 TON / GRAM", "🎮 Донат в игры", "🔐 VPN"]
            for i, t in enumerate(cats):
                s.add(Category(title=t, sort=i))
            await s.flush()
            vpn_cat = await s.scalar(select(Category.id).where(Category.title == "🔐 VPN"))
            s.add(Product(category_id=vpn_cat, title="VPN 30 дней", price=199, kind="vpn",
                          vpn_days=30, vpn_gb=0, description="Безлимитный трафик, автоматическая выдача ключа."))
            await s.commit()
