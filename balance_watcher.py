import asyncio
import logging

import httpx
from aiogram import Bot

import config
from admin_alerts import notify_admin_once, resolve_admin_alert

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1440 * 60  # раз в сутки
LOW_BALANCE_THRESHOLD_USD = 10.0

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"

async def watch_openrouter_balance(client: httpx.AsyncClient, management_key: str, bot: Bot) -> None:
    logger.info("Мониторинг баланса OpenRouter запущен")
    while True:
        try:
            response = await client.get(
                OPENROUTER_CREDITS_URL,
                headers={"Authorization": f"Bearer {management_key}"},
            )
            response.raise_for_status()
            data = response.json()["data"]
            remaining = data["total_credits"] - data["total_usage"]

            if remaining < LOW_BALANCE_THRESHOLD_USD:
                await notify_admin_once(
                    bot,
                    "openrouter_low_balance",
                    f"⚠️ На OpenRouter реально осталось ${remaining:.2f}. "
                    f"Пополните: https://openrouter.ai/settings/credits",
                )
            else:
                resolve_admin_alert("openrouter_low_balance")

        except Exception:
            logger.exception("Не удалось проверить баланс OpenRouter")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)