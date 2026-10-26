"""Фоновая задача для подписок. Запускается из main.py рядом с watch_openrouter_balance.

Каждые 15 секунд: проверяет статусы неоплаченных платежей.
Каждую минуту: списывает деньги с подписок, которым пора продлеваться,
и отправляет напоминания о скором автосписании.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update

import config
import subscription_service as svc
from database import YookassaPayment, async_session

logger = logging.getLogger(__name__)

POLL_INTERVAL = 15   # секунд между проверками статусов
SLOW_INTERVAL = 60   # секунд между автосписаниями и напоминаниями


def _fmt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(config.SUBSCRIPTION_TZ).strftime("%d.%m.%Y %H:%M")


def _title(tier: str) -> str:
    return config.SUBSCRIPTION_TIERS.get(tier, {}).get("title", tier)


def _message_for(o: svc.PaymentOutcome) -> tuple[str, InlineKeyboardMarkup | None]:
    t = _title(o.tier)
    until = _fmt(o.expires_at)

    if o.kind == "activated":
        return (
            f"✅ Подписка <b>{t}</b> активна до {until} (МСК).\n\n"
            f"Она продлится автоматически. За {config.RENEWAL_NOTICE_HOURS} ч. до списания "
            f"пришлю напоминание. Отключить автопродление можно в любой момент: /subscription"
        ), None
    if o.kind == "activated_no_autorenew":
        return (
            f"✅ Подписка <b>{t}</b> активна до {until} (МСК).\n\n"
            f"Автопродление не подключилось: выбранный способ оплаты его не поддерживает. "
            f"После окончания подписку нужно будет оплатить заново."
        ), None
    if o.kind == "renewed":
        return f"✅ Подписка <b>{t}</b> продлена до {until} (МСК). Списано {o.amount_rub} ₽.", None
    if o.kind == "first_failed":
        return "❌ Платёж не прошёл. Попробуйте ещё раз: /subscription", None
    if o.kind == "renew_failed":
        return (
            f"⚠️ Не удалось продлить подписку <b>{t}</b>: списание {o.amount_rub} ₽ не прошло.\n"
            f"Попробую ещё раз через {config.RENEWAL_RETRY_INTERVAL_HOURS} ч. "
            f"(осталось попыток: {o.attempts_left}). Проверьте, что на карте достаточно средств."
        ), None
    if o.kind == "renew_gave_up":
        return (
            f"❌ Не удалось продлить подписку <b>{t}</b>, автопродление отключено.\n"
            f"Оформить заново: /subscription"
        ), None
    if o.kind == "need_confirmation":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить платёж", url=o.url)]
        ])
        return (
            f"🔐 Для продления подписки <b>{t}</b> ({o.amount_rub} ₽) банк просит "
            f"подтвердить платёж. Нажмите кнопку ниже."
        ), kb
    if o.kind == "manual_review":
        return (
            "Оплата получена, но подписку не удалось активировать автоматически. "
            "Мы разберёмся вручную и напишем вам."
        ), None
    return "", None


async def _send(bot: Bot, chat_id: int, text: str, markup=None) -> None:
    if not text:
        return
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        logger.warning("Не удалось отправить сообщение %s: %r", chat_id, e)


async def _notify(bot: Bot, o: svc.PaymentOutcome) -> None:
    text, markup = _message_for(o)
    await _send(bot, o.telegram_id, text, markup)
    if o.kind == "manual_review" and config.ADMIN_ID:
        await _send(
            bot, config.ADMIN_ID,
            f"⚠️ Нужен ручной разбор: user={o.telegram_id}, {o.tier}/{o.period_days}, "
            f"{o.amount_rub} ₽. Подробности в logs/bot.log (CRITICAL).",
        )


# ───────────── Опрос незавершённых платежей ─────────────

async def _poll_pending(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    ttl = timedelta(hours=config.PENDING_PAYMENT_TTL_HOURS)

    async with async_session() as session:
        rows = (await session.execute(
            select(
                YookassaPayment.yookassa_payment_id,
                YookassaPayment.created_at,
                YookassaPayment.last_checked_at,
            )
            .where(YookassaPayment.status == "pending")
            .order_by(YookassaPayment.created_at)
            .limit(200)
        )).all()

    due: list[str] = []
    stale: list[str] = []
    for pid, created, checked in rows:
        age = now - created
        if age > ttl:
            stale.append(pid)
            continue
        # Свежие платежи проверяем часто, старые реже
        gap = timedelta(seconds=10) if age < timedelta(minutes=15) else timedelta(minutes=5)
        if checked is None or now - checked >= gap:
            due.append(pid)

    sem = asyncio.Semaphore(5)

    async def one(pid: str) -> None:
        try:
            async with sem:
                outcome = await svc.process_payment(pid)
            if outcome:
                await _notify(bot, outcome)
        except Exception:
            logger.exception("Сбой обработки платежа %s", pid)

    await asyncio.gather(*(one(p) for p in due + stale))

    if stale:
        # Последняя проверка выше уже сделана; кто всё ещё pending, тот просрочен
        async with async_session() as session:
            async with session.begin():
                await session.execute(
                    update(YookassaPayment)
                    .where(
                        YookassaPayment.yookassa_payment_id.in_(stale),
                        YookassaPayment.status == "pending",
                    )
                    .values(status="expired", processed_at=now)
                )


async def _renew_and_remind(bot: Bot) -> None:
    for outcome in await svc.charge_due_renewals():
        await _notify(bot, outcome)

    for n in await svc.pop_renewal_notices():
        if n["price"] is None:
            continue
        await _send(
            bot, n["telegram_id"],
            f"🔔 Подписка <b>{_title(n['tier'])}</b> действует до {_fmt(n['expires_at'])} (МСК) "
            f"и продлится автоматически: спишем {n['price']} ₽"
        )


async def subscription_worker(bot: Bot) -> None:
    last_slow = 0.0
    while True:
        try:
            await _poll_pending(bot)
            if time.monotonic() - last_slow >= SLOW_INTERVAL:
                last_slow = time.monotonic()
                await _renew_and_remind(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой в subscription_worker")
        await asyncio.sleep(POLL_INTERVAL)
