import os
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
load_dotenv()


def _get_required(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Переменная окружения {key} не найдена. "
            f"Проверьте, что файл .env существует и содержит {key}."
        )
    return value

SUBSCRIPTION_TZ = ZoneInfo("Europe/Moscow")
SUBSCRIPTION_TIERS = {
    "basic": {
        "title": "Basic",
        "emoji": "🔹",
        "daily_limit": 3,
        "prices": {7: 179, 30: 390, 90: 990},
    },
    "pro": {
        "title": "Pro",
        "emoji": "⚡",
        "daily_limit": 7,
        "prices": {7: 279, 30: 590, 90: 1490},
    },
}

PERIOD_LABELS = {7: "7 дней", 30: "1 месяц", 90: "3 месяца"}
def get_subscription_price(tier: str, period_days: int) -> int | None:
    """Цена в рублях или None, если такого тарифа/периода нет."""
    tier_cfg = SUBSCRIPTION_TIERS.get(tier)
    if tier_cfg is None:
        return None
    return tier_cfg["prices"].get(period_days)

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
OFFER_URL = os.getenv("OFFER_URL", "")   # ссылка на оферту
RENEWAL_NOTICE_HOURS = 24                # за сколько часов напоминать о списании
PENDING_PAYMENT_TTL_HOURS = 24           # через сколько неоплаченный платёж считается просроченным
PAYMENT_LINK_REUSE_MINUTES = 10
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
YOOKASSA_RETURN_URL = f"https://t.me/{BOT_USERNAME}"
YOOKASSA_PROVIDER_TOKEN = os.getenv("YOOKASSA_PROVIDER_TOKEN", "")
YOOKASSA_VAT_CODE = 1  # 1 = без НДС, для самозанятых

RENEWAL_RETRY_INTERVAL_HOURS = 24
RENEWAL_MAX_ATTEMPTS = 3
RENEWAL_CHARGE_LEAD_HOURS = 1

TELEGRAM_BOT_TOKEN = _get_required("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = _get_required("GEMINI_API_KEY")
DATABASE_URL = _get_required("DATABASE_URL")

NANO_BANANA_MODEL = os.getenv("NANO_BANANA_MODEL", "gemini-3-pro-image")
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1K")
ASPECT_RATIO = os.getenv("ASPECT_RATIO", "1:1")
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# Только для разработки: подменяет вызов провайдера картинок, деньги OpenRouter не тратятся.
# "off"  - обычная работа
# "ok"   - притворяемся, что генерация прошла успешно
# "fail" - притворяемся, что провайдер временно недоступен (проверка возврата)
DEV_FAKE_GENERATION = os.getenv("DEV_FAKE_GENERATION", "off")

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
