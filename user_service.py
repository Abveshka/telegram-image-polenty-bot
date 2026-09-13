from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from database import async_session, User, Transaction
import config


async def get_or_create_user(telegram_id: int, username: str | None) -> User:
    """
    Находит пользователя по telegram_id, либо создаёт нового с балансом 0,
    если он пишет боту впервые. Это и есть "регистрация" — без отдельной
    команды, прозрачно для пользователя.
    """
    async with async_session() as session:
        # Два сообщения от нового пользователя могут прийти почти одновременно.
        # INSERT .. ON CONFLICT не допускает падения на UNIQUE telegram_id.
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
        if user is None:  # Защита от неконсистентного состояния БД.
            raise RuntimeError(f"Не удалось получить пользователя {telegram_id}")
        return user


async def get_balance(telegram_id: int) -> int:
    user = await get_or_create_user(telegram_id, username=None)
    return user.balance


async def has_enough_balance(telegram_id: int) -> bool:
    balance = await get_balance(telegram_id)
    return balance >= config.PRICE_PER_GENERATION_STARS


async def charge_for_generation(telegram_id: int) -> bool:
    """
    Списывает стоимость одной генерации с баланса пользователя
    и записывает это как транзакцию. Вызывается ТОЛЬКО после того,
    как изображение уже успешно сгенерировано — чтобы не списывать
    деньги за неудачную попытку.
    """
    async with async_session() as session:
        # Условие в UPDATE делает списание атомарным. Иначе несколько
        # параллельных генераций могли бы списать баланс ниже нуля.
        user_id = await session.scalar(
            update(User)
            .where(
                User.telegram_id == telegram_id,
                User.balance >= config.PRICE_PER_GENERATION_STARS,
            )
            .values(balance=User.balance - config.PRICE_PER_GENERATION_STARS)
            .returning(User.id)
        )
        if user_id is None:
            await session.rollback()
            return False

        session.add(Transaction(
            user_id=user_id,
            amount=-config.PRICE_PER_GENERATION_STARS,
            type="generation",
        ))
        await session.commit()
        return True


async def credit_balance(telegram_id: int, username: str | None, stars_amount: int) -> None:
    """
    Начисляет звёзды на баланс после успешной оплаты через Telegram Stars.
    """
    user = await get_or_create_user(telegram_id, username)

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one()

        user.balance += stars_amount
        session.add(Transaction(
            user_id=user.id,
            amount=stars_amount,
            type="payment",
        ))
        await session.commit()
