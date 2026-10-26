import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

import config
import yookassa_client as yk
from database import Subscription, User, YookassaPayment, async_session
from user_service import get_or_create_user, grant_subscription

logger = logging.getLogger(__name__)


class SubscriptionError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


@dataclass
class PaymentOutcome:
    """Результат обработки платежа: по нему воркер решает, что написать пользователю."""
    kind: str  # activated / activated_no_autorenew / renewed / first_failed /
    #            renew_failed / renew_gave_up / need_confirmation / manual_review
    telegram_id: int
    tier: str = ""
    period_days: int = 0
    amount_rub: int = 0
    expires_at: datetime | None = None
    attempts_left: int = 0
    url: str | None = None


async def get_subscription(telegram_id: int) -> Subscription | None:
    async with async_session() as session:
        return await session.scalar(
            select(Subscription)
            .join(User, User.id == Subscription.user_id)
            .where(User.telegram_id == telegram_id)
        )


async def start_subscription_payment(
    telegram_id: int, username: str | None, tier: str, period_days: int
) -> str:
    """Создаёт платёж с сохранением карты. Возвращает ссылку на оплату."""
    price = config.get_subscription_price(tier, period_days)
    if price is None:
        raise SubscriptionError("Такого тарифа нет. Откройте меню подписки заново.")

    user = await get_or_create_user(telegram_id, username)
    now = datetime.now(timezone.utc)

    async with async_session() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == user.id))
        sub_id = sub.id if sub else None
        if sub and sub.expires_at > now and sub.tier == tier and sub.period_days == period_days:
            name = config.SUBSCRIPTION_TIERS[tier]["title"]
            raise SubscriptionError(
                f"У вас уже активна подписка {name} на этот же срок."
            )

        # Свежую неоплаченную ссылку на тот же тариф отдаём повторно
        since = now - timedelta(minutes=config.PAYMENT_LINK_REUSE_MINUTES)
        existing_url = await session.scalar(
            select(YookassaPayment.confirmation_url)
            .where(
                YookassaPayment.user_id == user.id,
                YookassaPayment.purpose == "subscription_first",
                YookassaPayment.tier == tier,
                YookassaPayment.period_days == period_days,
                YookassaPayment.status == "pending",
                YookassaPayment.created_at >= since,
                YookassaPayment.confirmation_url.is_not(None),
            )
            .order_by(YookassaPayment.created_at.desc())
            .limit(1)
        )
        if existing_url:
            return existing_url

    title = config.SUBSCRIPTION_TIERS[tier]["title"]
    key = str(uuid.uuid4())
    payment = await yk.create_payment(
        amount_rub=price,
        description=f"Подписка {title}, {config.PERIOD_LABELS[period_days]}",
        metadata={
            "user_id": user.id,
            "telegram_id": telegram_id,
            "tier": tier,
            "period_days": period_days,
            "purpose": "subscription_first",
        },
        save_payment_method=True,
        idempotence_key=key,
    )
    url = payment["confirmation"]["confirmation_url"]

    async with async_session() as session:
        async with session.begin():
            session.add(YookassaPayment(
                yookassa_payment_id=payment["id"],
                user_id=user.id,
                subscription_id=sub_id,
                purpose="subscription_first",
                tier=tier,
                period_days=period_days,
                amount_rub=price,
                idempotence_key=key,
                confirmation_url=url,
            ))
    return url


def _apply_renewal_failure(sub: Subscription, now: datetime) -> tuple[str, int]:
    """Фиксирует неудачную попытку автосписания. Возвращает (kind, осталось попыток)."""
    sub.failed_attempts += 1
    if sub.failed_attempts >= config.RENEWAL_MAX_ATTEMPTS:
        sub.auto_renew = False
        sub.next_charge_at = None
        return "renew_gave_up", 0
    sub.next_charge_at = now + timedelta(hours=config.RENEWAL_RETRY_INTERVAL_HOURS)
    return "renew_failed", config.RENEWAL_MAX_ATTEMPTS - sub.failed_attempts


async def process_payment(yk_payment_id: str) -> PaymentOutcome | None:
    """Узнаёт у ЮKassa итог платежа и применяет его. Безопасно вызывать повторно."""
    try:
        data = await yk.get_payment(yk_payment_id)
    except yk.YooKassaError as e:
        logger.warning("Не удалось получить платёж %s: %s", yk_payment_id, e)
        return None

    status = data.get("status")
    now = datetime.now(timezone.utc)

    async with async_session() as session:
        async with session.begin():
            row = await session.scalar(
                select(YookassaPayment)
                .where(YookassaPayment.yookassa_payment_id == yk_payment_id)
                .with_for_update()
            )
            if row is None or row.processed_at is not None:
                return None  # неизвестный или уже обработанный платёж

            row.last_checked_at = now
            if status not in ("succeeded", "canceled"):
                return None  # ещё в процессе

            telegram_id = await session.scalar(
                select(User.telegram_id).where(User.id == row.user_id)
            )
            row.processed_at = now

            if status == "canceled":
                reason = (data.get("cancellation_details") or {}).get("reason") or ""
                row.status = "canceled"
                row.cancellation_reason = reason[:64]
                return await _on_failed(session, row, telegram_id, reason, now)

            pm = data.get("payment_method") or {}
            row.status = "succeeded"
            return await _on_success(
                session, row, telegram_id, pm.get("id") if pm.get("saved") else None
            )


async def _on_success(session, row, telegram_id, pm_id) -> PaymentOutcome:
    renewal = row.purpose == "subscription_renewal"
    base = dict(tier=row.tier, period_days=row.period_days, amount_rub=row.amount_rub)

    prev_auto = None
    if renewal:
        prev_auto = await session.scalar(
            select(Subscription.auto_renew).where(Subscription.user_id == row.user_id)
        )

    try:
        expires_at = await grant_subscription(
            session, row.user_id, row.tier, row.period_days, payment_method_id=pm_id
        )
    except (ValueError, KeyError):
        logger.critical(
            "ДЕНЬГИ ПОЛУЧЕНЫ, ПОДПИСКА НЕ ВЫДАНА: payment=%s user=%s tier=%s",
            row.yookassa_payment_id, row.user_id, row.tier, exc_info=True,
        )
        return PaymentOutcome("manual_review", telegram_id, **base)

    sub = await session.scalar(select(Subscription).where(Subscription.user_id == row.user_id))
    row.subscription_id = sub.id

    if renewal and prev_auto is False:
        # Пользователь отключил автопродление, пока платёж был в пути
        sub.auto_renew = False
        sub.next_charge_at = None
        return PaymentOutcome("renewed", telegram_id, expires_at=expires_at, **base)

    if not sub.payment_method_id:
        # Способ оплаты не сохранился (например, оплатили через СБП)
        sub.auto_renew = False
        sub.next_charge_at = None
        return PaymentOutcome("activated_no_autorenew", telegram_id, expires_at=expires_at, **base)

    kind = "renewed" if renewal else "activated"
    return PaymentOutcome(kind, telegram_id, expires_at=expires_at, **base)


async def _on_failed(session, row, telegram_id, reason, now) -> PaymentOutcome | None:
    base = dict(tier=row.tier, period_days=row.period_days, amount_rub=row.amount_rub)

    if row.purpose != "subscription_renewal":
        if reason == "expired_on_confirmation":
            return None  # человек просто не оплатил: не беспокоим
        return PaymentOutcome("first_failed", telegram_id, **base)

    sub = await session.scalar(
        select(Subscription).where(Subscription.user_id == row.user_id).with_for_update()
    )
    if sub is None or not sub.auto_renew:
        return None
    kind, left = _apply_renewal_failure(sub, now)
    return PaymentOutcome(kind, telegram_id, attempts_left=left, **base)


# ───────────── Автопродление ─────────────

async def _delay_charge(sub_id: int, *, minutes: int = 0, hours: int = 0) -> None:
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                update(Subscription)
                .where(Subscription.id == sub_id)
                .values(next_charge_at=datetime.now(timezone.utc) + timedelta(minutes=minutes, hours=hours))
            )


async def charge_due_renewals(limit: int = 50) -> list[PaymentOutcome]:
    """Списывает деньги с подписок, у которых наступил next_charge_at."""
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        rows = (await session.execute(
            select(
                Subscription.id, Subscription.user_id, Subscription.tier,
                Subscription.period_days, Subscription.payment_method_id,
                Subscription.renewal_number, Subscription.failed_attempts,
                User.telegram_id,
            )
            .join(User, User.id == Subscription.user_id)
            .where(
                Subscription.auto_renew.is_(True),
                Subscription.payment_method_id.is_not(None),
                Subscription.next_charge_at <= now,
            )
            .order_by(Subscription.next_charge_at)
            .limit(limit)
        )).all()

    outcomes: list[PaymentOutcome] = []
    for row in rows:
        try:
            outcome = await _charge_one(row)
        except Exception:
            logger.exception("Сбой автосписания: sub=%s", row.id)
            continue
        if outcome:
            outcomes.append(outcome)
    return outcomes


async def _charge_one(row) -> PaymentOutcome | None:
    now = datetime.now(timezone.utc)
    price = config.get_subscription_price(row.tier, row.period_days)
    if price is None:
        logger.error("Нет цены для подписки sub=%s (%s/%s)", row.id, row.tier, row.period_days)
        await _delay_charge(row.id, hours=24)
        return None

    # Не создаём второй платёж, пока предыдущий не завершился
    async with async_session() as session:
        pending = await session.scalar(
            select(YookassaPayment.id).where(
                YookassaPayment.subscription_id == row.id,
                YookassaPayment.purpose == "subscription_renewal",
                YookassaPayment.status == "pending",
            )
        )
    if pending is not None:
        return None

    # Ключ детерминированный: повтор после сбоя не создаст дубль платежа
    key = f"renew-{row.id}-{row.renewal_number}-{row.failed_attempts}"
    title = config.SUBSCRIPTION_TIERS[row.tier]["title"]

    try:
        payment = await yk.create_payment(
            amount_rub=price,
            description=f"Продление подписки {title}, {config.PERIOD_LABELS[row.period_days]}",
            metadata={
                "user_id": row.user_id,
                "telegram_id": row.telegram_id,
                "tier": row.tier,
                "period_days": row.period_days,
                "purpose": "subscription_renewal",
            },
            payment_method_id=row.payment_method_id,
            idempotence_key=key,
        )
    except yk.YooKassaError as e:
        logger.error("Автосписание sub=%s не создалось: %s", row.id, e)
        if e.status_code and 400 <= e.status_code < 500:
            # ЮKassa отклонила запрос (например, способ оплаты больше не действует)
            async with async_session() as session:
                async with session.begin():
                    sub = await session.scalar(
                        select(Subscription).where(Subscription.id == row.id).with_for_update()
                    )
                    kind, left = _apply_renewal_failure(sub, now)
            return PaymentOutcome(
                kind, row.telegram_id, tier=row.tier, period_days=row.period_days,
                amount_rub=price, attempts_left=left,
            )
        await _delay_charge(row.id, minutes=10)  # сеть или сбой ЮKassa: повторим скоро
        return None

    url = (payment.get("confirmation") or {}).get("confirmation_url")
    async with async_session() as session:
        async with session.begin():
            session.add(YookassaPayment(
                yookassa_payment_id=payment["id"],
                user_id=row.user_id,
                subscription_id=row.id,
                purpose="subscription_renewal",
                tier=row.tier,
                period_days=row.period_days,
                amount_rub=price,
                idempotence_key=key,
                confirmation_url=url,
            ))
            # Временно откладываем: итог (успех или неудача) выставит process_payment
            await session.execute(
                update(Subscription)
                .where(Subscription.id == row.id)
                .values(next_charge_at=now + timedelta(hours=config.RENEWAL_RETRY_INTERVAL_HOURS))
            )

    if url and payment.get("status") == "pending":
        # Банк просит подтверждение (3-D Secure): нужен пользователь
        return PaymentOutcome(
            "need_confirmation", row.telegram_id, tier=row.tier,
            period_days=row.period_days, amount_rub=price, url=url,
        )
    return await process_payment(payment["id"])


# ───────────── Напоминания и отмена ─────────────

async def pop_renewal_notices(limit: int = 100) -> list[dict]:
    """Подписки, о скором автосписании которых нужно напомнить. Сразу помечает их."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=config.RENEWAL_NOTICE_HOURS)
    result = []
    async with async_session() as session:
        async with session.begin():
            rows = (await session.execute(
                select(Subscription, User.telegram_id)
                .join(User, User.id == Subscription.user_id)
                .where(
                    Subscription.auto_renew.is_(True),
                    Subscription.payment_method_id.is_not(None),
                    Subscription.renewal_notified_at.is_(None),
                    Subscription.expires_at > now,
                    Subscription.expires_at <= horizon,
                )
                .limit(limit)
            )).all()
            for sub, telegram_id in rows:
                sub.renewal_notified_at = now
                result.append({
                    "telegram_id": telegram_id,
                    "tier": sub.tier,
                    "period_days": sub.period_days,
                    "price": config.get_subscription_price(sub.tier, sub.period_days),
                    "expires_at": sub.expires_at,
                })
    return result


async def cancel_auto_renew(telegram_id: int) -> bool:
    """Отключает автопродление и забывает сохранённый способ оплаты.
    Подписка продолжает действовать до конца оплаченного срока."""
    async with async_session() as session:
        async with session.begin():
            user_id = await session.scalar(select(User.id).where(User.telegram_id == telegram_id))
            if user_id is None:
                return False
            res = await session.execute(
                update(Subscription)
                .where(Subscription.user_id == user_id, Subscription.auto_renew.is_(True))
                .values(auto_renew=False, next_charge_at=None, payment_method_id=None)
                .returning(Subscription.id)
            )
            return res.first() is not None
