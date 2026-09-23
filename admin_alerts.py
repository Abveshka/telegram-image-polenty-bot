# admin_alerts.py
import logging

from aiogram import Bot

import config

logger = logging.getLogger(__name__)

_active_alerts: set[str] = set()  # какие алерты сейчас "открыты", чтобы не дублировать


async def notify_admin_once(bot: Bot, alert_key: str, text: str) -> None:
    if alert_key in _active_alerts:
        return

    try:
        await bot.send_message(config.ADMIN_ID, text)
        _active_alerts.add(alert_key)
    except Exception:
        logger.exception("Не удалось отправить уведомление админу (alert_key=%s)", alert_key)


def resolve_admin_alert(alert_key: str) -> None:
    _active_alerts.discard(alert_key)