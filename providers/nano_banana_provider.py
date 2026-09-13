import asyncio
from google import genai
from google.genai import errors as genai_errors

from providers.base import ImageProvider, ImageGenerationError
import config


class NanoBananaProvider(ImageProvider):
    """
    Реальный провайдер: генерирует изображение через Nano Banana Pro
    (gemini-3-pro-image) — платный вызов Gemini API.
    """

    def __init__(self):
        self._client = genai.Client(api_key=config.GEMINI_API_KEY)

    async def generate(self, prompt: str) -> bytes:
        try:
            # Клиент google-genai синхронный, а generate() у нас async.
            # Выносим блокирующий вызов в отдельный поток через
            # asyncio.to_thread, чтобы не морозить приём сообщений бота
            # на время ожидания ответа от Gemini (может быть несколько секунд).
            interaction = await asyncio.to_thread(
                self._client.interactions.create,
                model=config.NANO_BANANA_MODEL,
                input=prompt,
                response_format={
                    "type": "image",
                    "mime_type": "image/png",
                    "aspect_ratio": config.ASPECT_RATIO,
                    "image_size": config.IMAGE_SIZE,
                },
            )

            if not interaction.output_image or not interaction.output_image.data:
                raise ImageGenerationError(
                    "Gemini API не вернул изображение (пустой ответ)."
                )

            import base64
            return base64.b64decode(interaction.output_image.data)

        except genai_errors.APIError as e:
            raise ImageGenerationError(f"Ошибка Gemini API: {e}") from e
        except Exception as e:
            raise ImageGenerationError(f"Непредвиденная ошибка генерации: {e}") from e