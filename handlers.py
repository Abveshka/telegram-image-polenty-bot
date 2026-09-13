import logging
from aiogram import Router, F
from aiogram.types import ErrorEvent, Message
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile

import config
from user_service import get_or_create_user, get_balance, has_enough_balance, charge_for_generation
from providers.base import ImageGenerationError
from providers.mock_provider import MockImageProvider
from providers.nano_banana_provider import NanoBananaProvider

logger = logging.getLogger(__name__)

router = Router()

# Выбор провайдера происходит один раз при старте бота,
# на основе переменной USE_MOCK_IMAGE из .env.
# Именно это и есть "переключение одной переменной окружения"
# из исходной архитектуры проекта.
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


@router.message(F.text)
async def handle_prompt(message: Message) -> None:
    prompt = message.text
    telegram_id = message.from_user.id
    username = message.from_user.username

    await get_or_create_user(telegram_id, username)

    if not await has_enough_balance(telegram_id):
        balance = await get_balance(telegram_id)
        await message.answer(
            f"Недостаточно звёзд для генерации.\n"
            f"Ваш баланс: {balance} ★\n"
            f"Стоимость генерации: {config.PRICE_PER_GENERATION_STARS} ★\n\n"
            f"Пополнить баланс: /buy"
        )
        return

    status_message = await message.answer("Генерирую изображение...")

    try:
        image_bytes = await provider.generate(prompt)
    except ImageGenerationError as e:
        logger.warning(f"Ошибка генерации для промпта {prompt!r}: {e}")
        await status_message.edit_text(
            "Не получилось сгенерировать изображение. Попробуйте ещё раз "
            "или измените запрос. Звёзды не были списаны."
        )
        return

    if not await charge_for_generation(telegram_id):
        # Баланс мог измениться, пока провайдер генерировал изображение.
        # Не отправляем результат бесплатно при параллельных запросах.
        await status_message.edit_text(
            "Пока выполнялась генерация, баланс изменился и звёзд уже "
            "недостаточно. Звёзды за эту попытку не списаны."
        )
        return

    photo = BufferedInputFile(image_bytes, filename="generated.png")
    await message.answer_photo(photo, caption=f"«{prompt}»")
    await status_message.delete()


@router.error()
async def log_update_error(event: ErrorEvent) -> bool:
    """Пишет полный traceback, чтобы ошибка апдейта не терялась в консоли."""
    logger.exception("Необработанная ошибка при обработке Telegram update", exc_info=event.exception)
    return True
