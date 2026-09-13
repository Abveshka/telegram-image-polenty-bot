from datetime import datetime
from sqlalchemy import String, BigInteger, ForeignKey, Numeric, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

import config


class Base(DeclarativeBase):
    """Базовый класс для всех таблиц (моделей) SQLAlchemy."""
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    balance: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[int]  # положительное = пополнение, отрицательное = списание
    type: Mapped[str] = mapped_column(String(32))  # "payment" или "generation"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="transactions")


# SQLAlchemy с asyncpg требует префикс "postgresql+asyncpg://", а не просто
# "postgresql://" — поэтому подменяем префикс из DATABASE_URL на лету,
# чтобы в .env оставался стандартный, привычный формат строки подключения.
_async_db_url = config.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(_async_db_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """
    Создаёт таблицы в БД, если их ещё нет.
    Вызывается один раз при старте бота (см. bot.py).

    Для реального продакшена лучше заменить на alembic-миграции,
    но на старте разработки это самый быстрый способ.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)