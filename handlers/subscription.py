import logging
from datetime import datetime, timezone
from aiogram.exceptions import TelegramBadRequest
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, Message, PreCheckoutQuery,
)

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


def _format_generations(n: int) -> str:
    """Возвращает число с правильным окончанием слова 'генерация'."""
    if 11 <= n % 100 <= 19:
        return f"{n} генераций"

    last_digit = n % 10
    if last_digit == 1:
        return f"{n} генерация"
    elif 2 <= last_digit <= 4:
        return f"{n} генерации"
    else:
        return f"{n} генераций"


async def _main_screen(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    sub = await svc.get_subscription(telegram_id)
    lines = ["💳 <b>Подписка</b>", "", "Ежедневный лимит генераций, токены не тратятся:"]
    for cfg in config.SUBSCRIPTION_TIERS.values():
        limit_text = _format_generations(cfg['daily_limit'])
        lines.append(f"{cfg['emoji']} <b>{cfg['title']}</b>: {limit_text} в день")

    active = _is_active(sub)
    if active:
        t = config.SUBSCRIPTION_TIERS[sub.tier]
        period = config.PERIOD_LABELS.get(sub.period_days, f"{sub.period_days} дней")
        auto = "включено" if sub.auto_renew else "выключено"
        lines += ["", f"✅ Ваша подписка: <b>{t['title']}</b> на <b>{period}</b>  до {_fmt(sub.expires_at)} (МСК)",
                  f"Автопродление: {auto}"]
    else: lines += ["", f"Оформите подписку прямо сейчас 👇"]

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
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.warning("edit_text не удался: %r", e)
    except Exception:
        logger.exception("edit_text: неожиданная ошибка")


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

    await callback.answer()
    rows = []
    for days, price in cfg["prices"].items():
        label = f"{config.PERIOD_LABELS[days]} — {price} ₽"
        stars = config.get_subscription_stars_price(tier, days)
        if stars:
            label += f" / {stars} ⭐"
        rows.append([_btn(label, f"buy:sub:m:{tier}:{days}")])
    rows.append([_btn("🔙 Назад", "buy:subscription")])
    limit_text = _format_generations(cfg['daily_limit'])
    text = f"{cfg['emoji']} <b>{cfg['title']}</b>: {limit_text} в день\n\nВыберите период:"
    await _safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


def _parse_tier_days(data: str, prefix: str) -> tuple[str, int] | None:
    try:
        parts = data.removeprefix(prefix).split(":")
        if len(parts) != 2:
            return None
        tier, days_s = parts
        days = int(days_s)
    except ValueError as e:
        return None

    if config.get_subscription_price(tier, days) is None:
        return None

    return tier, days


@router.callback_query(F.data.startswith("buy:sub:m:"))
async def cb_method(callback: CallbackQuery) -> None:
    parsed = _parse_tier_days(callback.data or "", "buy:sub:m:")
    if parsed is None:
        await callback.answer("Тариф недоступен", show_alert=True)
        return
    tier, days = parsed

    sub = await svc.get_subscription(callback.from_user.id)
    if _is_active(sub) and sub.tier == tier and sub.period_days == days:
        await callback.answer("У вас уже активна подписка этого тарифа на этот срок", show_alert=True)
        return

    await callback.answer()
    cfg = config.SUBSCRIPTION_TIERS[tier]
    price = config.get_subscription_price(tier, days)
    stars = config.get_subscription_stars_price(tier, days)

    text = (
        f"<b>💎 Подписка {cfg['title']}</b>\n"
        f"🗓 {config.PERIOD_LABELS[days]} • ⚡ {cfg['daily_limit']} ген/день"
    )

    warn = svc.burn_warning(sub, tier, days)
    if warn:
        text += f"\n\n⚠️ <i>{warn}</i>"
        if stars:
            text += "\n<i>При оплате звёздами автопродление будет отключено.</i>"

    rows = [[_btn(f"💳 Картой — {price} ₽ (автопродление)", f"buy:sub:c:{tier}:{days}")]]
    if stars:
        rows.append([_btn(f"⭐ Звёздами — {stars} ⭐ (без автопродления)", f"buy:sub:s:{tier}:{days}")])
    rows.append([_btn("🔙 Назад", f"buy:sub:t:{tier}")])

    await _safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data.startswith("buy:sub:c:"))
async def cb_consent(callback: CallbackQuery) -> None:
    parsed = _parse_tier_days(callback.data or "", "buy:sub:c:")
    if parsed is None:
        await callback.answer("Тариф недоступен", show_alert=True)
        return
    tier, days = parsed
    price = config.get_subscription_price(tier, days)
    if price is None:
        await callback.answer("Тариф недоступен", show_alert=True)
        return

    await callback.answer()
    sub = await svc.get_subscription(callback.from_user.id)
    warn = svc.burn_warning(sub, tier, days)

    title = config.SUBSCRIPTION_TIERS[tier]["title"]
    offer = f' и <a href="{config.OFFER_URL}">офертой</a>' if config.OFFER_URL else ""
    text = (
            f"<b>Подписка {title}</b>\n"
            f"Период: {config.PERIOD_LABELS[days]}\n"
            f"Стоимость: {price} ₽\n\n"
            + (f"{warn}\n\n" if warn else "")
            + f"🔁 Подписка продлевается автоматически на тот же срок: каждые "
              f"{config.PERIOD_LABELS[days]} с вашей карты будет списываться {price} ₽, "
              f"пока вы не отключите автопродление (в любой момент: /subscription).\n\n"
              f"Нажимая «Согласен, оплатить», вы соглашаетесь с условиями автопродления{offer}."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Согласен, оплатить", f"buy:sub:go:{tier}:{days}")],
        [_btn("🔙 Назад", f"buy:sub:m:{tier}:{days}")],  # <-- теперь на экран способа
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

def _parse_sub_payload(payload: str) -> tuple[str, int] | None:
    try:
        prefix, tier, days_s = payload.split(":")
        if prefix != "sub":
            return None
        return tier, int(days_s)
    except (ValueError, AttributeError):
        return None


async def _notify_admin(bot, text: str) -> None:
    if not config.ADMIN_ID:
        logger.critical("Нужен ручной разбор, но ADMIN_ID не задан: %s", text)
        return
    try:
        await bot.send_message(config.ADMIN_ID, text)
    except Exception as e:
        logger.warning("Не удалось написать админу: %r", e)


@router.callback_query(F.data.startswith("buy:sub:s:"))
async def cb_pay_stars(callback: CallbackQuery) -> None:
    parsed = _parse_tier_days(callback.data or "", "buy:sub:s:")
    if parsed is None or callback.message is None:
        await callback.answer("Тариф недоступен", show_alert=True)
        return
    tier, days = parsed

    try:
        stars = await svc.validate_stars_purchase(callback.from_user.id, tier, days)
    except svc.SubscriptionError as e:
        await callback.answer(str(e), show_alert=True)
        return

    cfg = config.SUBSCRIPTION_TIERS[tier]

    await callback.message.answer_invoice(
        title=f"💎 Подписка {cfg['title']}"[:32],
        description=(
            f"🗓 {config.PERIOD_LABELS[days]} "
            f"• ⚡ {cfg['daily_limit']} ген/день "
            f"• 🛡️ Без автопродления"
        ),
        payload=f"sub:{tier}:{days}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Подписка {cfg['title']}"[:32], amount=stars)],
    )


@router.pre_checkout_query(F.invoice_payload.startswith("sub:"))
async def pre_checkout_sub(query: PreCheckoutQuery) -> None:
    parsed = _parse_sub_payload(query.invoice_payload)
    if parsed is None:
        await query.answer(ok=False, error_message="Тариф недоступен, откройте /subscription заново")
        return
    tier, days = parsed
    try:
        price = await svc.validate_stars_purchase(query.from_user.id, tier, days)
    except svc.SubscriptionError as e:
        await query.answer(ok=False, error_message=str(e))
        return
    if query.currency != "XTR" or query.total_amount != price:
        await query.answer(ok=False, error_message="Цена изменилась, откройте /subscription заново")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment.invoice_payload.startswith("sub:"))
async def on_sub_stars_paid(message: Message) -> None:
    sp = message.successful_payment
    if sp is None or message.from_user is None:
        return
    user = message.from_user
    parsed = _parse_sub_payload(sp.invoice_payload)
    info = f"user={user.id} payload={sp.invoice_payload} charge_id={sp.telegram_payment_charge_id}"

    if parsed is None:
        logger.critical("ЗВЁЗДЫ С НЕИЗВЕСТНЫМ PAYLOAD: %s", info)
        await message.answer("Оплата получена, но не удалось определить тариф. Мы разберёмся вручную.")
        await _notify_admin(message.bot, f"⚠️ Звёзды с неизвестным payload\n{info}")
        return
    tier, days = parsed

    try:
        out = await svc.activate_subscription_with_stars(
            telegram_id=user.id,
            username=user.username,
            tier=tier,
            period_days=days,
            amount_stars=sp.total_amount,
            charge_id=sp.telegram_payment_charge_id,
            provider_charge_id=sp.provider_payment_charge_id,
        )
    except Exception:
        logger.critical("ЗВЁЗДЫ ПОЛУЧЕНЫ, ПОДПИСКА НЕ ВЫДАНА (сбой): %s", info, exc_info=True)
        await message.answer("Оплата получена, но при активации произошёл сбой. Мы разберёмся и активируем вручную.")
        await _notify_admin(message.bot, f"⚠️ Сбой активации подписки за звёзды\n{info}")
        return

    if out.kind == "duplicate":
        return  # уже обработано, второй раз не пишем

    if out.kind == "manual_review":
        await message.answer("Оплата получена, но подписку не удалось активировать автоматически. Мы разберёмся вручную и напишем вам.")
        await _notify_admin(
            message.bot,
            f"⚠️ Нужен ручной разбор (звёзды)\nuser: {user.id}\n"
            f"тариф: {tier} / {days} дн., {sp.total_amount} ⭐\n"
            f"charge_id: {sp.telegram_payment_charge_id}",
        )
        return

    title = config.SUBSCRIPTION_TIERS[tier]["title"]
    await message.answer(
        f"✅ Подписка <b>{title}</b> активна до {_fmt(out.expires_at)} (МСК).\n\n"
        f"Оплата звёздами не продлевается автоматически: после окончания подписку нужно "
        f"оформить заново: /subscription"
    )