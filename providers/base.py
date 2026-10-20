from abc import ABC, abstractmethod

class ImageGenerationError(Exception):
    """Любая ошибка генерации."""

class ProviderOutOfFundsError(ImageGenerationError):
    """На счёте провайдера кончились деньги (HTTP 402)."""

class ProviderTemporaryError(ImageGenerationError):
    """Временный сбой (429, 5xx, обрыв соединения), повтор имеет смысл."""

class ImageRejectedError(ImageGenerationError):
    """Модель или модерация отказались генерировать. Картинки нет."""

class ImageProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        reference_image: bytes | None = None,
        media_type: str = "image/jpeg",
    ) -> bytes:
        raise NotImplementedError