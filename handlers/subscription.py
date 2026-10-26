import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import subscription_service as svc
import yookassa_client as yk

logger = logging.getLogger(__name__)
router = Router(name="subscription")


def _fmt(dt: datetime) -> str:
    return dt.astimezone(config.SUBSCRIPTION_TZ).strftime("%d.%m.%Y %H:%M")


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _is_active(sub) -> bool:
    return sub is not None and sub.expires_at > datetime.now(timezone.utc)


async def _main_screen(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    sub = await svc.get_subscription(telegram_id)
    lines = ["💳 <b>Подписка</b>", "", "Ежедневный лимит генераций, токены не тратятся:"]
    for cfg in config.SUBSCRIPTION_TIERS.values():
        lines.append(f"{cfg['emoji']} <b>{cfg['title']}</b>: {cfg['daily_limit']} в день")

    active = _is_active(sub)
    if active:
        t = config.SUBSCRIPTION_TIERS[sub.tier]
        auto = "включено" if sub.auto_renew else "выключено"
        lines += ["", f"✅ Ваша подписка: <b>{t['title']}</b> до {_fmt(sub.expires_at)} (МСК)",
                  f"Автопродление: {auto}"]

    rows = [
        [_btn(f"{cfg['emoji']} {cfg['title']}", f"buy:sub:t:{tier}")]
        for tier, cfg in config.SUBSCRIPTION_TIERS.items()
    ]
    if active and sub.auto_renew:
        rows.append([_btn("🚫 Отключить автопродление", "buy:sub:off")])
    rows.append([_btn("🔙 Назад", "buy:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:  # например, «message is not modified»
        logger.debug("edit_text: %r", e)


# ───────────── Навигация ─────────────

@router.message(Command("subscription"))
async def cmd_subscription(message: Message) -> None:
    if message.from_user is None:
        return
    text, kb = await _main_screen(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "buy:subscription")
async def cb_main(callback: CallbackQuery) -> None:
    await callback.answer()
    text, kb = await _main_screen(callback.from_user.id)
    await _safe_edit(callback, text, kb)


@router.callback_query(F.data.startswith("buy:sub:t:"))
async def cb_tier(callback: CallbackQuery) -> None:
    tier = (callback.data or "").removeprefix("buy:sub:t:")
    cfg = config.SUBSCRIPTION_TIERS.get(tier)
    if cfg is None:
        await callback.answer("Тариф недоступен", show_alert=True)
        return

    sub = await svc.get_subscription(callback.from_user.id)
    if _is_active(sub) and sub.tier != tier:
        name = config.SUBSCRIPTION_TIERS[sub.tier]["title"]
        await callback.answer(
            f"У вас активна подписка {name}. Сменить тариф можно после её окончания.",
            show_alert=True,
        )
        return

    await callback.answer()
    rows = [
        [_btn(f"{config.PERIOD_LABELS[days]} — {price} ₽", f"buy:sub:c:{tier}:{days}")]
        for days, price in cfg["prices"].items()
    ]
    rows.append([_btn("🔙 Назад", "buy:subscription")])
    text = f"{cfg['emoji']} <b>{cfg['title']}</b>: {cfg['daily_limit']} генераций в день\n\nВыберите период:"
    await _safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("buy:sub:c:"))
async def cb_consent(callback: CallbackQuery) -> None:
    try:
        tier, days_s = (callback.data or "").removeprefix("buy:sub:c:").split(":")
        days = int(days_s)
    except ValueError:
        await callback.answer("Откройте меню заново", show_alert=True)
        return
    price = config.get_subscription_price(tier, days)
    if price is None:
        await callback.answer("Тариф недоступен", show_alert=True)
        return

    await callback.answer()
    title = config.SUBSCRIPTION_TIERS[tier]["title"]
    offer = f' и <a href="{config.OFFER_URL}">офертой</a>' if config.OFFER_URL else ""
    text = (
        f"<b>Подписка {title}</b>\n"
        f"Период: {config.PERIOD_LABELS[days]}\n"
        f"Стоимость: {price} ₽\n\n"
        f"🔁 Подписка продлевается автоматически на тот же срок: каждые "
        f"{config.PERIOD_LABELS[days]} с вашей карты будет списываться {price} ₽, "
        f"пока вы не отключите автопродление (в любой момент: /subscription). "
        f"За {config.RENEWAL_NOTICE_HOURS} ч. до списания я пришлю напоминание.\n\n"
        f"Нажимая «Согласен, оплатить», вы соглашаетесь с условиями автопродления{offer}."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Согласен, оплатить", f"buy:sub:go:{tier}:{days}")],
        [_btn("🔙 Назад", f"buy:sub:t:{tier}")],
    ])
    await _safe_edit(callback, text, kb)


@router.callback_query(F.data.startswith("buy:sub:go:"))
async def cb_go(callback: CallbackQuery) -> None:
    try:
        tier, days_s = (callback.data or "").removeprefix("buy:sub:go:").split(":")
        days = int(days_s)
    except ValueError:
        await callback.answer("Откройте меню заново", show_alert=True)
        return

    try:
        url = await svc.start_subscription_payment(
            callback.from_user.id, callback.from_user.username, tier, days
        )
    except svc.SubscriptionError as e:
        await callback.answer(str(e), show_alert=True)
        return
    except yk.YooKassaError:
        logger.exception("Не удалось создать платёж подписки")
        await callback.answer("Не удалось создать платёж. Попробуйте позже.", show_alert=True)
        return

    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=url)],
        [_btn("🔙 Назад", "buy:subscription")],
    ])
    await _safe_edit(
        callback,
        "Нажмите кнопку, чтобы оплатить. После оплаты подписка активируется "
        "автоматически (обычно в течение минуты), я напишу вам.",
        kb,
    )


# ───────────── Отключение автопродления ─────────────

@router.callback_query(F.data == "buy:sub:off")
async def cb_off_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Да, отключить", "buy:sub:off:yes")],
        [_btn("Нет, оставить", "buy:subscription")],
    ])
    await _safe_edit(
        callback,
        "Отключить автопродление? Подписка продолжит действовать до конца "
        "оплаченного срока, но дальше списаний не будет.",
        kb,
    )


@router.callback_query(F.data == "buy:sub:off:yes")
async def cb_off(callback: CallbackQuery) -> None:
    done = await svc.cancel_auto_renew(callback.from_user.id)
    await callback.answer("Автопродление отключено" if done else "Оно уже отключено")
    text, kb = await _main_screen(callback.from_user.id)
    await _safe_edit(callback, text, kb)
