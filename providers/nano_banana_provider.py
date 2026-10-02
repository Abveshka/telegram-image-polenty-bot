import asyncio
import base64
import logging
import random
from typing import Any
import httpx

from providers.base import ImageGenerationError, ImageProvider

logger = logging.getLogger(__name__)

OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"
IMAGE_MODEL = "google/gemini-3.1-flash-image"

MAX_RETRIES = 2
BASE_BACKOFF = 1.5  # секунды
MAX_CONCURRENT_REQUESTS = 15  # общий лимит одновременных запросов к OpenRouter

class NanoBananaProvider(ImageProvider):
    """Генерация изображений через Gemini (Nano Banana 2) по API OpenRouter."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0),
            limits=httpx.Limits(
                max_connections=MAX_CONCURRENT_REQUESTS * 2,
                max_keepalive_connections=MAX_CONCURRENT_REQUESTS,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(
            self,
            prompt: str,
            reference_image: bytes | None = None,
            media_type: str = "image/jpeg",
    ) -> bytes:
        async with self._semaphore:
            return await self._generate_with_retries(
                prompt,
                reference_image,
                media_type,
            )

    async def _generate_with_retries(
            self,
            prompt: str,
            reference_image: bytes | None,
            media_type: str,
    ) -> bytes:
        last_error: Exception | None = None

        payload: dict[str, Any] = {
            "model": IMAGE_MODEL,
            "prompt": prompt,
        }

        if reference_image is not None:
            image_b64 = base64.b64encode(reference_image).decode("ascii")
            payload["input_references"] = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{image_b64}",
                    },
                }
            ]

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self._client.post(
                    OPENROUTER_IMAGES_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

                if response.status_code == 402:
                    # Кончились деньги на счету OpenRouter — ретраить бессмысленно
                    raise ImageGenerationError(
                        f"Недостаточно средств на счету OpenRouter: {response.text[:300]}"
                    )

                if response.status_code == 429 or response.status_code >= 500:
                    # Временная перегрузка — можно повторить попытку
                    raise ImageGenerationError(
                        f"OpenRouter временно недоступен ({response.status_code}): "
                        f"{response.text[:300]}"
                    )

                response.raise_for_status()
                data = response.json()
                image_b64 = data["data"][0]["b64_json"]
                return base64.b64decode(image_b64)

            except (httpx.TimeoutException, httpx.TransportError, ImageGenerationError) as error:
                if "Недостаточно средств" in str(error):
                    raise  # это не временная проблема, ретрай не поможет
                last_error = error
            except (KeyError, IndexError, ValueError) as error:
                # Неожиданный формат ответа — ретраить бесполезно
                raise ImageGenerationError(f"Некорректный ответ OpenRouter: {error}") from error

            if attempt < MAX_RETRIES:
                delay = BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "Retry %s/%s генерации после ошибки: %s", attempt + 1, MAX_RETRIES, last_error
                )
                await asyncio.sleep(delay)

        raise last_error or ImageGenerationError("Неизвестная ошибка генерации")
