from datetime import datetime
from sqlalchemy import String, BigInteger, ForeignKey, Numeric, DateTime, func, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

import config


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    balance: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")
    rejected_streak: Mapped[int] = mapped_column(default=0, server_default="0")
    block_count: Mapped[int] = mapped_column(default=0, server_default="0")
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """
        rejected_streak: сколько отказов модерации подряд у пользователя. Растёт на 1 при каждом отказе. Обнуляется в двух случаях: при установке бана и при успешной генерации.
        block_count: сколько банов уже было в текущей «серии эскалации». Он выбирает ступень лестницы: 0 → 10 минут, 1 → 1 час, 2 и больше → 24 часа. Растёт на 1 при каждом бане.
        blocked_until: когда закончится текущий бан. По нему get_block_remaining решает, пускать ли пользователя. Он же служит точкой отсчёта для 7-дневного сброса.
    """
class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("generation_id", "type", name="uq_transactions_generation_type"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[int]
    type: Mapped[str] = mapped_column(String(32))  # "payment" / "generation" / "refund" / "compensation"
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="transactions")
    generation_id: Mapped[int | None] = mapped_column(
        ForeignKey("generations.id", name="fk_transactions_generation_id"),
        nullable=True,
        index=True,
    )


class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[int] = mapped_column(primary_key=True)
    gen_id: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))  # started / success / failed / delivery_failed / rejected
    with_photo: Mapped[bool] = mapped_column(default=False)
    prompt: Mapped[str] = mapped_column(String(2000))
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

_async_db_url = config.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(_async_db_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)