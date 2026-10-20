import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import insert, select, update, delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from database import async_session, User, Transaction, Generation
import config

logger = logging.getLogger(__name__)

async def get_or_create_user(telegram_id: int, username: str | None) -> User:
    async with async_session() as session:
        created = await session.scalar(
            pg_insert(User)
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


async def compensate_generation(
    gen_id: str, status: str, error: Exception, count_as_reject: bool = False
) -> datetime | None:
    """Идемпотентно возвращает токены за генерацию и закрывает её.
    Возвращает новый rejected_streak (если count_as_reject), иначе None.
    Повторный вызов или генерация уже закрыта -> ничего не делает, вернёт None.
    """
    async with async_session() as session:
        async with session.begin():
            # 1. Условный переход started -> status: выигрывает ровно один вызов
            row = (await session.execute(
                update(Generation)
                .where(Generation.gen_id == gen_id, Generation.status == "started")
                .values(
                    status=status,
                    error_type=type(error).__name__,
                    error_text=str(error)[:500],
                    finished_at=func.now(),
                )
                .returning(Generation.id, Generation.user_id)
            )).first()
            if row is None:
                return None  # уже финализирована
            generation_pk, user_id = row

            # 2. Сколько списали за эту генерацию
            charged = await session.scalar(
                select(Transaction.amount).where(
                    Transaction.generation_id == generation_pk,
                    Transaction.type == "generation",
                )
            )
            if charged is None:
                logger.error("Нет транзакции списания: gen=%s", gen_id)
                return None
            amount = -charged

            # 3. Транзакция компенсации; конфликт по charge_id = уже компенсировано
            inserted = await session.scalar(
                pg_insert(Transaction)
                .values(
                    user_id=user_id,
                    amount=amount,
                    type="compensation",
                    generation_id=generation_pk,
                    telegram_payment_charge_id=f"compensation_{gen_id}",
                )
                .on_conflict_do_nothing()
                .returning(Transaction.id)
            )
            if inserted is not None:
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(balance=User.balance + amount)
                )

            # 4. Серия отказов модерации
            if count_as_reject:
                return await _register_reject(session, user_id)
    return None

async def credit_balance(
    telegram_id: int,
    username: str | None,
    stars_amount: int,
    charge_id: str,
    tx_type: str = "payment",
) -> bool:
    """Начисляет кредит ровно один раз. Возвращает False, если charge_id уже был."""
    user = await get_or_create_user(telegram_id, username)

    async with async_session() as session:
        try:
            async with session.begin_nested():
                await session.execute(
                    insert(Transaction).values(
                        user_id=user.id,
                        amount=stars_amount,
                        type=tx_type,
                        telegram_payment_charge_id=charge_id,
                    )
                )
        except IntegrityError:
            return False

        await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(balance=User.balance + stars_amount)
        )
        await session.commit()
        return True

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

async def begin_generation(
    gen_id: str, telegram_id: int, prompt: str, with_photo: bool
) -> bool:
    """Атомарно: списание + запись Generation + Transaction.
    Возвращает False, если не хватает токенов (ничего не изменено)."""
    price = config.PRICE_PER_GENERATION_STARS
    async with async_session() as session:
        async with session.begin():
            user_id = await session.scalar(
                update(User)
                .where(User.telegram_id == telegram_id, User.balance >= price)
                .values(balance=User.balance - price)
                .returning(User.id)
            )
            if user_id is None:
                return False

            generation_pk = await session.scalar(
                insert(Generation)
                .values(
                    gen_id=gen_id,
                    user_id=user_id,
                    status="started",
                    with_photo=with_photo,
                    prompt=prompt[:2000],
                )
                .returning(Generation.id)
            )

            await session.execute(
                insert(Transaction).values(
                    user_id=user_id,
                    amount=-price,
                    type="generation",
                    generation_id=generation_pk,
                )
            )
        return True


BAN_THRESHOLD = 3
BAN_LADDER = [timedelta(minutes=10), timedelta(hours=1), timedelta(hours=24)]
BLOCK_COUNT_RESET = timedelta(days=7)


async def _register_reject(session, user_id: int) -> datetime | None:
    """Увеличивает серию отказов; при пороге ставит бан. Возвращает blocked_until или None."""
    streak, block_count, blocked_until = (await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(rejected_streak=User.rejected_streak + 1)
        .returning(User.rejected_streak, User.block_count, User.blocked_until)
    )).one()

    if streak < BAN_THRESHOLD:
        return None

    now = datetime.now(timezone.utc)
    # Эскалация обнуляется, если с конца прошлого бана прошло 7+ дней
    if blocked_until is not None and now - blocked_until > BLOCK_COUNT_RESET:
        block_count = 0

    until = now + BAN_LADDER[min(block_count, len(BAN_LADDER) - 1)]
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(rejected_streak=0, block_count=block_count + 1, blocked_until=until)
    )
    return until

async def finish_generation(gen_id: str, status: str, error: Exception | None = None) -> None:
    async with async_session() as session:
        async with session.begin():
            row = (await session.execute(
                update(Generation)
                .where(Generation.gen_id == gen_id, Generation.status == "started")
                .values(
                    status=status,
                    error_type=type(error).__name__ if error else None,
                    error_text=str(error)[:500] if error else None,
                    finished_at=func.now(),
                )
                .returning(Generation.user_id)
            )).first()
            if row is not None and status == "success":
                await session.execute(
                    update(User).where(User.id == row[0]).values(rejected_streak=0)
                )

async def get_block_remaining(telegram_id: int) -> timedelta | None:
    async with async_session() as session:
        until = await session.scalar(
            select(User.blocked_until).where(User.telegram_id == telegram_id)
        )
    if until is None:
        return None
    remaining = until - datetime.now(timezone.utc)
    return remaining if remaining.total_seconds() > 0 else None