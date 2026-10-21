from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import config


def buy_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            # [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy:subscription")],
            [InlineKeyboardButton(text="💎 Купить токены", callback_data="buy:tokens")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="buy:main")],
        ]
    )


def payment_method_keyboard(credit: int, rub_price: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {rub_price} RUB 🇷🇺", callback_data=f"buy:rub:{credit}")],
            [InlineKeyboardButton(text="🌟 Telegram Stars", callback_data=f"buy:stars:{credit}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="buy:tokens")],
        ]
    )


def packages_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=package["label"],
            callback_data=f"buy:pkg:{package['credit_stars']}",
        )]
        for package in config.STAR_PACKAGES
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="buy:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Пополнить баланс", callback_data="menu:buy")],
            [InlineKeyboardButton(text="🖼 Генерация картинок", callback_data="menu:generate")],
            [InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance")],
        ]
    )

def start_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Начать диалог", callback_data="menu:dialog"),
                InlineKeyboardButton(text="📋 Меню", callback_data="buy:main"),
            ],
            [InlineKeyboardButton(text="🖼 Генерация картинок", callback_data="menu:generate")],
            [InlineKeyboardButton(text="💎 Пополнить баланс", callback_data="menu:buy")],
            [InlineKeyboardButton(text="💎 Оформить подписку ", callback_data="menu:buy")],
        ]
    )

def balance_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Пополнить баланс", callback_data="menu:buy")],
            [InlineKeyboardButton(text="🖼 Генерация картинок", callback_data="menu:generate")],
        ]
    )

def payment_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖼 Генерация картинок", callback_data="menu:generate")],
        ]
    )