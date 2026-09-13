from abc import ABC, abstractmethod


class ImageGenerationError(Exception):
    """
    Единое исключение для всех ошибок генерации изображений,
    независимо от того, какой провайдер их вызвал.

    Зачем: handlers.py будет ловить именно этот тип ошибки,
    не зная и не заботясь о том, упал ли Gemini API, мок-провайдер
    или что-то ещё в будущем (Kandinsky, FLUX и т.д.).
    """
    pass


class ImageProvider(ABC):
    """
    Абстрактный интерфейс провайдера генерации изображений.

    Любой провайдер (мок, Nano Banana, будущие альтернативы)
    обязан реализовать метод generate() с такой сигнатурой.
    Это позволяет handlers.py работать с любым провайдером
    одинаково, не зная деталей реализации.
    """

    @abstractmethod
    async def generate(self, prompt: str) -> bytes:
        """
        Генерирует изображение по текстовому промпту.

        :param prompt: текст от пользователя
        :return: байты изображения (например, PNG)
        :raises ImageGenerationError: если генерация не удалась
        """
        raise NotImplementedError