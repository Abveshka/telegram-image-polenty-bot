import logging
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import insert, select, update, delete, func, case, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from database import async_session, User, Transaction, Generation, Subscription
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


def _today_msk() -> date:
    return datetime.now(config.SUBSCRIPTION_TZ).date()


async def _try_spend_daily(session, user_id: int, today: date, now: datetime) -> bool:
    """Пытается списать одну дневную генерацию подписки. Одним атомарным UPDATE.
    True: списали. False: подписки нет, она истекла или лимит на сегодня выбран."""
    spent = await session.scalar(
        update(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.expires_at > now,
            or_(
                Subscription.daily_date.is_distinct_from(today),
                Subscription.daily_used < Subscription.daily_limit,
            ),
        )
        .values(
            daily_used=case(
                (Subscription.daily_date == today, Subscription.daily_used + 1),
                else_=1,  # новый день: начинаем с 1
            ),
            daily_date=today,
        )
        .returning(Subscription.id)
    )
    return spent is not None


async def _daily_left(session, user_id: int) -> int:
    """Сколько дневных генераций осталось сегодня (0, если подписки нет или она истекла)."""
    row = (await session.execute(
        select(Subscription.daily_limit, Subscription.daily_used, Subscription.daily_date)
        .where(
            Subscription.user_id == user_id,
            Subscription.expires_at > datetime.now(timezone.utc),
        )
    )).first()
    if row is None:
        return 0
    limit, used, day = row
    return limit if day != _today_msk() else max(limit - used, 0)

async def has_enough_balance(telegram_id: int) -> bool:
    price = config.PRICE_PER_GENERATION_STARS
    async with async_session() as session:
        row = (await session.execute(
            select(User.id, User.balance).where(User.telegram_id == telegram_id)
        )).first()
        if row is None:
            raise RuntimeError(
                f"has_enough_balance: пользователь {telegram_id} не найден — "
                f"вызовите get_or_create_user перед проверкой"
            )
        user_id, balance = row
        if balance >= price:
            return True
        return await _daily_left(session, user_id) > 0


async def compensate_generation(
    gen_id: str, status: str, error: Exception, count_as_reject: bool = False
) -> datetime | None:
    """Идемпотентно возвращает генерацию (в дневной лимит или токенами) и закрывает её"""
    async with async_session() as session:
        async with session.begin():
            row = (await session.execute(
                update(Generation)
                .where(Generation.gen_id == gen_id, Generation.status == "started")
                .values(
                    status=status,
                    error_type=type(error).__name__,
                    error_text=str(error)[:500],
                    finished_at=func.now(),
                )
                .returning(
                    Generation.id, Generation.user_id, Generation.source, Generation.sub_day
                )
            )).first()
            if row is None:
                return None
            generation_pk, user_id, source, sub_day = row

            # Сколько списали за эту генерацию (для подписочной это 0)
            charged = await session.scalar(
                select(Transaction.amount).where(
                    Transaction.generation_id == generation_pk,
                    Transaction.type == "generation",
                )
            )
            if charged is None:
                logger.error("Нет транзакции списания: gen=%s", gen_id)
                return None

            if source == "sub":
                # Возвращаем в дневной лимит, но только если счётчик всё ещё относится
                restored = await session.scalar(
                    update(Subscription)
                    .where(
                        Subscription.user_id == user_id,
                        Subscription.daily_date == sub_day,
                        Subscription.daily_used > 0,
                    )
                    .values(daily_used=Subscription.daily_used - 1)
                    .returning(Subscription.id)
                )
                if restored is not None:
                    amount, comp_source = 0, "sub"
                else:
                    # День сменился (лимит уже сгорел): компенсируем токенами
                    amount, comp_source = config.PRICE_PER_GENERATION_STARS, "balance"
            else:
                amount, comp_source = -charged, "balance"

            # Транзакция компенсации; конфликт по charge_id = уже компенсировано
            inserted = await session.scalar(
                pg_insert(Transaction)
                .values(
                    user_id=user_id,
                    amount=amount,
                    type="compensation",
                    generation_id=generation_pk,
                    telegram_payment_charge_id=f"compensation_{gen_id}",
                    source=comp_source,
                )
                .on_conflict_do_nothing()
                .returning(Transaction.id)
            )
            if inserted is not None and amount:
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(balance=User.balance + amount)
                )

            # Серия отказов модерации
            if count_as_reject:
                return await _register_reject(session, user_id)
    return None

async def grant_subscription(
    session,
    user_id: int,
    tier: str,
    period_days: int,
    payment_method_id: str | None = None,
) -> datetime:
    """Выдаёт или продлевает подписку после успешной оплаты. Возвращает новый expires_at.
    Работает в транзакции вызывающего: коммит делает он, вместе с отметкой платежа."""
    tier_cfg = config.SUBSCRIPTION_TIERS[tier]
    now = datetime.now(timezone.utc)
    lead = timedelta(hours=config.RENEWAL_CHARGE_LEAD_HOURS)

    sub = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_id).with_for_update()
    )

    # Если у пользователя вообще не было записи о подписке
    if sub is None:
        expires_at = now + timedelta(days=period_days)
        session.add(Subscription(
            user_id=user_id,
            tier=tier,
            period_days=period_days,
            daily_limit=tier_cfg["daily_limit"],
            expires_at=expires_at,
            auto_renew=True,
            payment_method_id=payment_method_id,
            next_charge_at=expires_at - lead,
            renewal_number=1,
            last_charge_at=now,
        ))
        return expires_at

    active = sub.expires_at > now

    # Запрещаем покупку ТОЛЬКО если тариф и срок точно такие же, и подписка еще активна
    if active and sub.tier == tier and sub.period_days == period_days:
        raise ValueError(f"У вас уже активна подписка {tier} на этот же срок.")

    # Для всех остальных вариантов (другой тариф, другой срок или истекшая подписка)
    # начинаем с чистого листа от текущего момента
    expires_at = now + timedelta(days=period_days)

    sub.tier = tier
    sub.period_days = period_days
    sub.daily_limit = tier_cfg["daily_limit"]
    sub.daily_used = 0
    sub.daily_date = None
    sub.expires_at = expires_at
    sub.auto_renew = True
    if payment_method_id is not None:
        sub.payment_method_id = payment_method_id
    sub.next_charge_at = expires_at - lead
    sub.failed_attempts = 0
    sub.renewal_number += 1
    sub.last_charge_at = now
    sub.renewal_notified_at = None
    return expires_at

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
    """Атомарно: списание + запись Generation + Transaction."""
    price = config.PRICE_PER_GENERATION_STARS
    now = datetime.now(timezone.utc)
    today = _today_msk()

    async with async_session() as session:
        async with session.begin():
            user_id = await session.scalar(
                select(User.id).where(User.telegram_id == telegram_id)
            )
            if user_id is None:
                return False

            if await _try_spend_daily(session, user_id, today, now):
                source, sub_day, charged = "sub", today, 0
            else:
                paid_user = await session.scalar(
                    update(User)
                    .where(User.id == user_id, User.balance >= price)
                    .values(balance=User.balance - price)
                    .returning(User.id)
                )
                if paid_user is None:
                    return False
                source, sub_day, charged = "balance", None, price

            generation_pk = await session.scalar(
                insert(Generation)
                .values(
                    gen_id=gen_id,
                    user_id=user_id,
                    status="started",
                    with_photo=with_photo,
                    prompt=prompt[:2000],
                    source=source,
                    sub_day=sub_day,
                )
                .returning(Generation.id)
            )

            await session.execute(
                insert(Transaction).values(
                    user_id=user_id,
                    amount=-charged,  # для подписочной генерации 0
                    type="generation",
                    generation_id=generation_pk,
                    source=source,
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