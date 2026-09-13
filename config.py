import os
from dotenv import load_dotenv

load_dotenv()


def _get_required(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Переменная окружения {key} не найдена. "
            f"Проверьте, что файл .env существует и содержит {key}."
        )
    return value


TELEGRAM_BOT_TOKEN = _get_required("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = _get_required("GEMINI_API_KEY")

USE_MOCK_IMAGE = os.getenv("USE_MOCK_IMAGE", "true").lower() == "true"
NANO_BANANA_MODEL = os.getenv("NANO_BANANA_MODEL", "gemini-3-pro-image")
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1K")
ASPECT_RATIO = os.getenv("ASPECT_RATIO", "1:1")