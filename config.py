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
DATABASE_URL = _get_required("DATABASE_URL")

NANO_BANANA_MODEL = os.getenv("NANO_BANANA_MODEL", "gemini-3-pro-image")
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1K")
ASPECT_RATIO = os.getenv("ASPECT_RATIO", "1:1")
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

YOOKASSA_PROVIDER_TOKEN = os.getenv("YOOKASSA_PROVIDER_TOKEN", "")  # тестовый токен из BotFather
# Код НДС для чека: 1 = без НДС.
YOOKASSA_VAT_CODE = 1

# Например: http://127.0.0.1:10809. Нужен, когда VPN работает в
# proxy-режиме, а не в TUN-режиме: aiohttp не использует системный прокси
# Windows автоматически.
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL") or None
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
OPENROUTER_MANAGEMENT_KEY = os.environ["OPENROUTER_MANAGEMENT_KEY"]

PRICE_PER_GENERATION_STARS = 10
STAR_PACKAGES = [
    {"label": "TEST 1 ★ → 1 генерация", "credit_stars": 10, "stars_to_pay": 1, "rub_to_pay": 1},
    {"label": "+4 Генерации | 119 руб/звезд", "credit_stars": 40, "stars_to_pay": 119, "rub_to_pay": 119},
    {"label": "+10 Генерации | 279 руб/звезд", "credit_stars": 100, "stars_to_pay": 279, "rub_to_pay": 279},
    {"label": "+25 Генерации | 649 руб/звезд", "credit_stars": 250, "stars_to_pay": 649, "rub_to_pay": 649},
    {"label": "+40 Генерации | 999 руб/звезд", "credit_stars": 400, "stars_to_pay": 999, "rub_to_pay": 999},
]
