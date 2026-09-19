import logging
from aiogram import Router, F
from aiogram.types import ErrorEvent, Message
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile
import time
import config
from user_service import (
    get_or_create_user,
    get_balance,
    has_enough_balance,
    charge_for_generation,
    refund_generation,
    credit_balance,
    reserve_payment_refund,
    undo_payment_refund,

)
from providers.base import ImageGenerationError
from providers.mock_provider import MockImageProvider
from providers.nano_banana_provider import NanoBananaProvider
from aiogram.types import LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command, CommandObject

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

GENERATION_COOLDOWN_SECONDS = 10
_active_generations: set[int] = set()
_last_generation_at: dict[int, float] = {}

BUY_COOLDOWN_SECONDS = 10
_last_buy_at: dict[int, float] = {}

logger = logging.getLogger(__name__)

router = Router()

PACKAGES_BY_PAYLOAD = {f"stars_{p['credit_stars']}": p for p in config.STAR_PACKAGES}

if config.USE_MOCK_IMAGE:
    provider = MockImageProvider()
    logger.info("Используется MockImageProvider (бесплатный тестовый режим)")
else:
    provider = NanoBananaProvider()
    logger.info("Используется NanoBananaProvider (платные запросы к Gemini API)")

@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    if message.from_user is not None:
        await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Привет! Отправь мне текстовое описание, и я сгенерирую по нему изображение.\n\n"
        "Например: «кот-космонавт на скейтборде»"
    )

async def report_failed_generation(
    bot: Bot, telegram_id: int, username: str | None, prompt: str, error: Exception | str
) -> None:
    """Фиксирует сбой генерации для ручной сверки. Деньги не возвращает."""
    logger.error(
        "СБОЙ ГЕНЕРАЦИИ (звёзды списаны): user=%s, username=%s, prompt=%r, error=%r",
        telegram_id, username, prompt, error,
    )
    try:
        await bot.send_message(
            config.ADMIN_ID,
            f"⚠️ Сбой генерации, звёзды списаны\n"
            f"user: {telegram_id} (@{username})\n"
            f"промпт: {prompt}\n"
            f"ошибка: {error!r}",
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление админу")

async def _run_generation(message: Message, prompt: str, bot: Bot) -> None:
    if message.from_user is None:
        return

    telegram_id = message.from_user.id
    username = message.from_user.username
    await get_or_create_user(telegram_id, username)

    if not await charge_for_generation(telegram_id):
        balance = await get_balance(telegram_id)
        await message.answer(
            f"Недостаточно звёзд для генерации.\n"
            f"Ваш баланс: {balance} ★\n"
            f"Стоимость генерации: {config.PRICE_PER_GENERATION_STARS} ★\n\n"
            "Пополнить баланс: /buy"
        )
        return

    status_message = await message.answer("Генерирую изображение...")

    try:
        image_bytes = await provider.generate(prompt)
    except ImageGenerationError as error:
        await report_failed_generation(bot, telegram_id, username, prompt, error)
        await status_message.edit_text(
            "Не получилось сгенерировать изображение. Попробуйте ещё раз "
            "или измените запрос."
        )
        return
    except Exception as error:
        await report_failed_generation(bot, telegram_id, username, prompt, error)
        await status_message.edit_text(
            "Произошла техническая ошибка при генерации. "
            "Информация передана администратору."
        )
        return

    try:
        photo = BufferedInputFile(image_bytes, filename="generated.png")
        await message.answer_photo(photo, caption=f"«{prompt}»")
    except Exception as error:
        await report_failed_generation(bot, telegram_id, username, prompt, error)
        await status_message.edit_text(
            "Не удалось отправить изображение. Информация передана администратору."
        )
        return

    await status_message.delete()

@router.message(Command("generate"))
async def handle_generate_prompt(message: Message, command: CommandObject, bot: Bot) -> None:
    prompt = command.args

    if not prompt:
        await message.answer(
            "Укажите текст после команды, например:\n"
            "/generate кот-космонавт на скейтборде"
        )
        return

    if message.from_user is None:
        return

    telegram_id = message.from_user.id

    if telegram_id in _active_generations:
        await message.answer("Предыдущая генерация ещё выполняется. Дождитесь результата.")
        return

    elapsed = time.monotonic() - _last_generation_at.get(telegram_id, 0.0)
    if elapsed < GENERATION_COOLDOWN_SECONDS:
        wait = int(GENERATION_COOLDOWN_SECONDS - elapsed) + 1
        await message.answer(f"Слишком часто. Подождите {wait} сек.")
        return

    _active_generations.add(telegram_id)
    try:
        await _run_generation(message, prompt, bot)
    finally:
        _active_generations.discard(telegram_id)
        _last_generation_at[telegram_id] = time.monotonic()

@router.error()
async def log_update_error(event: ErrorEvent) -> bool:
    """Пишет полный traceback, чтобы ошибка апдейта не терялась в консоли."""
    logger.exception("Необработанная ошибка при обработке Telegram update", exc_info=event.exception)
    return True

@router.message(Command("buy"))
async def handle_buy(message: Message) -> None:
    if message.from_user is None:
        return

    user_id = message.from_user.id
    now = time.monotonic()
    last = _last_buy_at.get(user_id)
    if last is not None and now - last < BUY_COOLDOWN_SECONDS:
        await message.answer("Счета уже отправлены выше. Подождите немного перед повтором.")
        return
    _last_buy_at[user_id] = now

    for package in config.STAR_PACKAGES:
        await message.answer_invoice(
            title=package["label"],
            description=f"Пополнение баланса бота на {package['credit_stars']} ★",
            payload=f"stars_{package['credit_stars']}",
            currency="XTR",
            prices=[LabeledPrice(label=package["label"], amount=package["stars_to_pay"])],
        )


@router.pre_checkout_query()
async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    package = PACKAGES_BY_PAYLOAD.get(query.invoice_payload)
    if (
        package is None
        or query.currency != "XTR"
        or query.total_amount != package["stars_to_pay"]
    ):
        await query.answer(ok=False, error_message="Пакет недоступен, откройте /buy заново")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message) -> None:
    if message.from_user is None or message.successful_payment is None:
        return

    payment = message.successful_payment  # <-- добавлено
    payload = payment.invoice_payload
    credit_stars = int(payload.replace("stars_", ""))

    try:
        await credit_balance(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            stars_amount=credit_stars,
            charge_id=payment.telegram_payment_charge_id,
        )
    except Exception:
        logger.critical(
            "ОПЛАТА НЕ НАЧИСЛЕНА: user=%s, кредиты=%s, charge_id=%s",
            message.from_user.id, credit_stars, payment.telegram_payment_charge_id,
            exc_info=True,
        )
        await message.answer(
            "Оплата получена, но при начислении произошёл сбой. "
            "Мы разберёмся и начислим вручную."
        )
        return

    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"Оплата прошла успешно! Начислено {credit_stars} ★.\n"
        f"Текущий баланс: {balance} ★"
    )

@router.message(Command("balance"))
async def handle_balance(message: Message) -> None:
    if message.from_user is None:
        return
    await get_or_create_user(message.from_user.id, message.from_user.username)
    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"Баланс: {balance} ★ (≈ {balance // config.PRICE_PER_GENERATION_STARS} генераций)"
    )

@router.message(Command("refund"))
async def handle_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    # Только для вас: иначе любой пользователь сможет вернуть чужие платежи
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

    await message.answer(f"Готово: звёзды возвращены, с баланса списано {amount} ★")