import logging
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart

import config
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
    await message.answer(
        "Привет! Отправь мне текстовое описание, и я сгенерирую по нему изображение.\n\n"
        "Например: «кот-космонавт на скейтборде»"
    )


@router.message(F.text)
async def handle_prompt(message: Message) -> None:
    prompt = message.text

    status_message = await message.answer("Генерирую изображение...")

    try:
        image_bytes = await provider.generate(prompt)
    except ImageGenerationError as e:
        logger.warning(f"Ошибка генерации для промпта {prompt!r}: {e}")
        await status_message.edit_text(
            "Не получилось сгенерировать изображение. Попробуйте ещё раз "
            "или измените запрос."
        )
        return

    from aiogram.types import BufferedInputFile
    photo = BufferedInputFile(image_bytes, filename="generated.png")

    await message.answer_photo(photo, caption=f"«{prompt}»")
    await status_message.delete()