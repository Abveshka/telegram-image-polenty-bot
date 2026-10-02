from abc import ABC, abstractmethod

class ImageGenerationError(Exception):
    pass

class ImageProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        reference_image: bytes | None = None,
        media_type: str = "image/jpeg",
    ) -> bytes:
        raise NotImplementedError