import logging
import re
import time

import subscription_service as svc
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
import config
from subscription_worker import _fmt
from user_service import (
    credit_balance, get_balance, reserve_payment_refund, undo_payment_refund,
)

logger = logging.getLogger(__name__)
router = Router(name="admin")

def _is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == config.ADMIN_ID



#Возвращает звезды
@router.message(Command("refund_stars"))
async def handle_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    if not _is_admin(message):
        return

    if not command.args:
        await message.answer("Использование: /refund_stars <charge_id>")
        return

    charge_id = command.args.strip()

    try:
        telegram_id, amount = await reserve_payment_refund(charge_id)
    except ValueError as error:
        await message.answer(f"Возврат невозможен: {error}")
        return

    try:
        await bot.refund_star_payment(
            user_id=telegram_id,
            telegram_payment_charge_id=charge_id,
        )
    except TelegramAPIError as error:
        logger.exception("Telegram не вернул звёзды по платежу %s", charge_id)
        await undo_payment_refund(charge_id, telegram_id, amount)
        await message.answer(f"Telegram отклонил возврат, баланс восстановлен: {error}")
        return

    await message.answer(f"Готово: звёзды возвращены, с баланса списано {amount} токенов")

#Начисляет токены
@router.message(Command("compensate"))
async def handle_compensate(message: Message, command: CommandObject, bot: Bot) -> None:
    if not _is_admin(message):
        return

    usage = (
        "Использование: /compensate <telegram_id> <количество> [код_обращения]\n"
        "Например: /compensate 1498757244 10 a1b2c3d4"
    )
    if not command.args:
        await message.answer(usage)
        return

    parts = command.args.split()
    if len(parts) not in (2, 3) or not all(p.lstrip("-").isdigit() for p in parts[:2]):
        await message.answer(usage)
        return

    target_id, amount = int(parts[0]), int(parts[1])
    if amount <= 0:
        await message.answer("Количество должно быть положительным")
        return

    gen_id = parts[2].lower() if len(parts) == 3 else None
    if gen_id is not None and not re.fullmatch(r"[0-9a-f]{8}", gen_id):
        await message.answer("Код обращения должен состоять из 8 символов (0-9, a-f)")
        return

    if gen_id is not None:
        charge_id = f"compensation_{gen_id}"
    else:
        # Без кода защиты от повтора нет
        charge_id = f"compensation_{target_id}_{int(time.time())}"

    credited = await credit_balance(
        telegram_id=target_id,
        username=None,
        stars_amount=amount,
        charge_id=charge_id,
        tx_type="compensation",
    )
    if not credited:
        await message.answer(f"Генерация {gen_id} уже была компенсирована, повторно не начисляю.")
        return

    logger.info("Компенсация: user=%s amount=%s gen=%s", target_id, amount, gen_id)

    balance = await get_balance(target_id)
    await message.answer(f"Начислено {amount} токенов юзеру {target_id} (компенсация).\nЕго баланс теперь: {balance} токенов")

    try:
        await bot.send_message(
            target_id,
            f"Приносим извинения за техническую проблему с генерацией. "
            f"Вам начислено {amount} токенов в качестве компенсации.",
        )
    except Exception:
        logger.warning("Не удалось уведомить юзера %s о компенсации", target_id)


@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    """/refund <payment_id> [сумма] [revoke]"""
    if not _is_admin(message):
        return
    tokens = (command.args or "").split()
    if not tokens:
        await message.answer("Формат: /refund <yookassa_payment_id> [сумма] [revoke]")
        return

    amount = next((int(t) for t in tokens[1:] if t.isdigit()), None)
    revoke = "revoke" in tokens[1:]

    try:
        out = await svc.request_refund(tokens[0], amount, revoke=revoke, reason="Возврат")
    except svc.SubscriptionError as e:
        await message.answer(f"❌ {e}")
        return

    await message.answer(
        f"Возврат {out.amount_rub} ₽: {out.kind}"
        + (", подписка отозвана" if out.subscription_revoked else "")
        + ("\nЗапрос не завершён, выполните /refund_sync" if out.kind == "pending" else "")
    )
    await _notify_refund(bot, out)


@router.message(Command("refund_sync"))
async def cmd_refund_sync(message: Message, bot: Bot) -> None:
    if not _is_admin(message):
        return
    outs = await svc.sync_pending_refunds()
    await message.answer(f"Обработано возвратов: {len(outs)}")
    for out in outs:
        await _notify_refund(bot, out)


@router.message(Command("grant_sub"))
async def cmd_grant_sub(message: Message, command: CommandObject, bot: Bot) -> None:
    """/grant_sub <payment_id>: выдать подписку за платёж в статусе manual_review"""
    if not _is_admin(message):
        return
    pid = (command.args or "").strip()
    if not pid:
        await message.answer("Формат: /grant_sub <yookassa_payment_id>")
        return
    try:
        telegram_id, expires_at = await svc.manual_grant_for_payment(pid)
    except svc.SubscriptionError as e:
        await message.answer(f"❌ {e}")
        return

    await message.answer(f"✅ Подписка выдана до {_fmt(expires_at)} (МСК)")
    try:
        await bot.send_message(
            telegram_id,
            f"✅ Приносим извинения за задержку: ваша подписка активирована до "
            f"{_fmt(expires_at)} (МСК).",
        )
    except Exception:
        logger.warning("Не удалось уведомить %s о выдаче подписки", telegram_id)


async def _notify_refund(bot: Bot, out: svc.RefundOutcome) -> None:
    if out.kind == "refunded":
        text = (f"💸 Мы вернули вам {out.amount_rub} ₽. Деньги придут на карту "
                f"в течение нескольких дней (зависит от банка)."
                + (" Подписка отключена." if out.subscription_revoked else ""))
    else:
        return
    try:
        await bot.send_message(out.telegram_id, text)
    except Exception:
        logger.warning("Не удалось уведомить %s о возврате", out.telegram_id)


@router.message(Command("refund_stars_sub"))
async def cmd_refund_stars(message: Message, command: CommandObject, bot: Bot) -> None:
    """/refund_stars <telegram_charge_id> [revoke]"""
    if not _is_admin(message):
        return
    tokens = (command.args or "").split()
    if not tokens:
        await message.answer("Формат: /refund_stars <telegram_charge_id> [revoke]")
        return

    charge_id = tokens[0]
    revoke = "revoke" in tokens[1:]

    try:
        # ВЫЗЫВАЕМ ИМЕННО request_star_refund!
        out = await svc.request_star_refund(charge_id, revoke=revoke, bot=bot)
    except svc.SubscriptionError as e:
        await message.answer(f"❌ {e}")
        return

    await message.answer(
        f"✅ Возврат {out.amount_stars} ⭐ успешно выполнен"
        + ("\nПодписка отозвана." if out.subscription_revoked else "")
    )

    try:
        await bot.send_message(
            out.telegram_id,
            f"💸 Вам был выполнен возврат {out.amount_stars} ⭐."
            + (" Подписка аннулирована." if out.subscription_revoked else "")
        )
    except Exception:
        logger.warning("Не удалось уведомить пользователя %s о возврате звёзд", out.telegram_id)


@router.message(Command("grant_stars_sub"))
async def cmd_grant_stars_sub(message: Message, command: CommandObject, bot: Bot) -> None:
    """/grant_stars_sub <telegram_charge_id>: выдать подписку за звёздный платёж в manual_review"""
    if not _is_admin(message):
        return
    pid = (command.args or "").strip()
    if not pid:
        await message.answer("Формат: /grant_stars_sub <telegram_charge_id>")
        return

    try:
        telegram_id, expires_at = await svc.manual_grant_for_stars_payment(pid)
    except svc.SubscriptionError as e:
        await message.answer(f"❌ {e}")
        return

    await message.answer(f"✅ Подписка за звёзды выдана до {_fmt(expires_at)} (МСК)")
    try:
        await bot.send_message(
            telegram_id,
            f"✅ Приносим извинения за задержку: ваша подписка за звёзды активирована до "
            f"{_fmt(expires_at)} (МСК).",
        )
    except Exception:
        logger.warning("Не удалось уведомить %s о ручной выдаче подписки за звёзды", telegram_id)