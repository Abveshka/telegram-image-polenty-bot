import logging
from sqlalchemy import select, update, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from database import async_session, User, Transaction
import config

logger = logging.getLogger(__name__)

async def get_or_create_user(telegram_id: int, username: str | None) -> User:
    async with async_session() as session:
        created = await session.scalar(
            insert(User)
            .values(telegram_id=telegram_id, username=username, balance=0)
            .on_conflict_do_nothing(index_elements=[User.telegram_id])
            .returning(User)
        )
        if created is not None:
            await session.commit()
            return created

        user = await session.scalar(
            select(User).where(User.telegram_id == telegram_id)
        )
        if user is None:
            raise RuntimeError(f"Не удалось получить пользователя {telegram_id}")
        return user


async def get_balance(telegram_id: int) -> int:
    async with async_session() as session:
        balance = await session.scalar(
            select(User.balance).where(User.telegram_id == telegram_id)
        )
        if balance is None:
            raise RuntimeError(
                f"get_balance: пользователь {telegram_id} не найден — "
                f"вызовите get_or_create_user перед get_balance"
            )
        return balance


async def has_enough_balance(telegram_id: int) -> bool:
    balance = await get_balance(telegram_id)
    return balance >= config.PRICE_PER_GENERATION_STARS


async def charge_for_generation(telegram_id: int) -> bool:
    price = config.PRICE_PER_GENERATION_STARS
    async with async_session() as session:
        result = await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id, User.balance >= price)
            .values(balance=User.balance - price)
            .returning(User.id)
        )
        row = result.first()
        if row is None:
            return False

        await session.execute(
            insert(Transaction).values(user_id=row[0], amount=-price, type="generation")
        )
        await session.commit()
        return True

async def refund_generation(telegram_id: int) -> None:
    """Возвращает один заранее списанный кредит после ошибки генерации."""
    price = config.PRICE_PER_GENERATION_STARS
    async with async_session() as session:
        result = await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(balance=User.balance + price)
            .returning(User.id)
        )
        row = result.first()
        if row is None:
            logger.error(f"refund_generation: пользователь {telegram_id} не найден")
            return

        await session.execute(
            insert(Transaction).values(user_id=row[0], amount=price, type="refund")
        )
        await session.commit()


async def credit_balance(telegram_id: int, username: str | None, stars_amount: int, charge_id: str) -> None:
    """Начисляет кредит ровно один раз для уникального платежа Telegram."""
    user = await get_or_create_user(telegram_id, username)

    async with async_session() as session:
        try:
            async with session.begin_nested():
                await session.execute(
                    insert(Transaction).values(
                        user_id=user.id,
                        amount=stars_amount,
                        type="payment",
                        telegram_payment_charge_id=charge_id,
                    )
                )
        except IntegrityError:
            return

        await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(balance=User.balance + stars_amount)
        )

        await session.commit()

async def reserve_payment_refund(charge_id: str) -> tuple[int, int]:
    """
    Списывает с баланса кредиты за платёж и пишет транзакцию возврата.
    Возвращает (telegram_id, сумма). При проблеме бросает ValueError с понятным текстом.
    """
    async with async_session() as session:
        row = (
            await session.execute(
                select(Transaction.user_id, Transaction.amount, User.telegram_id)
                .join(User, User.id == Transaction.user_id)
                .where(
                    Transaction.telegram_payment_charge_id == charge_id,
                    Transaction.type == "payment",
                )
            )
        ).first()
        if row is None:
            raise ValueError("Платёж с таким charge_id не найден")
        user_id, amount, telegram_id = row

        try:
            async with session.begin_nested():
                await session.execute(
                    insert(Transaction).values(
                        user_id=user_id,
                        amount=-amount,
                        type="payment_refund",
                        # charge_id уникален, поэтому у возврата свой ключ.
                        # Он же защищает от повторного возврата.
                        telegram_payment_charge_id=f"refund:{charge_id}",
                    )
                )
        except IntegrityError:
            raise ValueError("Этот платёж уже возвращён")

        result = await session.execute(
            update(User)
            .where(User.id == user_id, User.balance >= amount)
            .values(balance=User.balance - amount)
            .returning(User.id)
        )
        if result.first() is None:
            await session.rollback()
            raise ValueError(
                "На балансе пользователя меньше, чем нужно списать "
                "(часть генераций уже потрачена)"
            )

        await session.commit()
        return telegram_id, amount


async def undo_payment_refund(charge_id: str, telegram_id: int, amount: int) -> None:
    """Откатывает reserve_payment_refund, если Telegram не смог вернуть звёзды."""
    async with async_session() as session:
        await session.execute(
            delete(Transaction).where(
                Transaction.telegram_payment_charge_id == f"refund:{charge_id}"
            )
        )
        await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(balance=User.balance + amount)
        )
        await session.commit()
