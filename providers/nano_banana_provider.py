import asyncio
import base64
import logging
import random
from typing import Any
import httpx

from providers.base import ImageGenerationError, ImageProvider, ProviderTemporaryError, ProviderOutOfFundsError, \
    ImageRejectedError

logger = logging.getLogger(__name__)

OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"
IMAGE_MODEL = "google/gemini-3.1-flash-image"

MAX_RETRIES = 1
BASE_BACKOFF = 1.5  # секунды
MAX_CONCURRENT_REQUESTS = 15  # общий лимит одновременных запросов к OpenRouter

def _error_info(response: httpx.Response) -> tuple[dict, dict]:
    """Достаёт error и error.metadata из тела ответа. Не падает на мусоре."""
    try:
        body = response.json()
    except ValueError:
        return {}, {}
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return {}, {}
    meta = err.get("metadata")
    return err, meta if isinstance(meta, dict) else {}


def _is_rejection(status: int, meta: dict) -> bool:
    if meta.get("error_type") in ("content_policy_violation", "refusal"):
        return True
    return status == 400 and bool(meta.get("finish_reason") or meta.get("block_reason"))

class NanoBananaProvider(ImageProvider):

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
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                # Запрос не дошёл до сервера, повтор безопасен
                last_error = ProviderTemporaryError(f"Не удалось подключиться: {error}")
            except httpx.TimeoutException as error:
                # Ждали ответ и не дождались: сервер мог начать генерацию, не повторяем
                raise ImageGenerationError("OpenRouter не ответил вовремя") from error
            except httpx.TransportError as error:
                last_error = ProviderTemporaryError(f"Сетевая ошибка: {error}")
            else:
                status = response.status_code

            if status < 400:
                try:
                    data = response.json()
                    if isinstance(data, dict) and data.get("error"):
                        raise ImageGenerationError(
                            f"OpenRouter вернул {status} с ошибкой: {response.text[:300]}"
                        )
                    return base64.b64decode(data["data"][0]["b64_json"])
                except ImageGenerationError:
                    raise
                except (KeyError, IndexError, TypeError, ValueError) as error:
                    raise ImageGenerationError(
                        f"Некорректный ответ OpenRouter ({status}): "
                        f"{error!r}; тело: {response.text[:300]}"
                    ) from error

            err, meta = _error_info(response)

            if _is_rejection(status, meta):
                reason = meta.get("finish_reason") or meta.get("block_reason") or meta.get("error_type")
                raise ImageRejectedError(
                    f"OpenRouter отклонил запрос ({status}, {reason}): "
                    f"{str(err.get('message', ''))[:200]}"
                )
            if status in (401, 402) or (status == 403 and meta.get("limit_source")):
                raise ProviderOutOfFundsError(
                    f"Нет доступа или средств на OpenRouter ({status}): {response.text[:300]}"
                )
            if status == 429 or status >= 500:
                last_error = ProviderTemporaryError(
                    f"OpenRouter временно недоступен ({status}): {response.text[:300]}"
                )
            else:
                raise ImageGenerationError(
                    f"OpenRouter вернул {status}: {response.text[:300]}"
                )

            if attempt < MAX_RETRIES:
                delay = BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "Retry %s/%s после ошибки: %s", attempt + 1, MAX_RETRIES, last_error
                )
                await asyncio.sleep(delay)

        raise last_error or ImageGenerationError("Неизвестная ошибка генерации")
