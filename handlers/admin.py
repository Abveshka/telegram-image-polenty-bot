import logging
import re
import time
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
import config
from user_service import (
    credit_balance, get_balance, reserve_payment_refund, undo_payment_refund,
)

logger = logging.getLogger(__name__)
router = Router(name="admin")

#Возвращает звезды
@router.message(Command("refund"))
async def handle_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    if message.from_user is None or message.from_user.id != config.ADMIN_ID:
        return

    if not command.args:
        await message.answer("Использование: /refund <charge_id>")
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
    if message.from_user is None or message.from_user.id != config.ADMIN_ID:
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