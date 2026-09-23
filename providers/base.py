from abc import ABC, abstractmethod

class ImageGenerationError(Exception):
    pass

class ImageProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str) -> bytes:
        raise NotImplementedError