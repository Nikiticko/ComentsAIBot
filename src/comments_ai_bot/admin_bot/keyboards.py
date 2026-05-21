from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


ADD_CHANNEL = "Добавить канал"
CHANNEL_LIST = "Список каналов"
READY_TO_COMMENT_POSTS = "Ready 20к+"
TEST_COMMENT = "Тест коммент"
TELEGRAM_AUTH = "Аккаунты TG"
LOGS = "Логи"
SETTINGS = "Настройки"
CANCEL = "Отмена"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADD_CHANNEL), KeyboardButton(text=CHANNEL_LIST)],
            [KeyboardButton(text=READY_TO_COMMENT_POSTS), KeyboardButton(text=TEST_COMMENT)],
            [KeyboardButton(text=TELEGRAM_AUTH), KeyboardButton(text=LOGS)],
            [KeyboardButton(text=SETTINGS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=CANCEL)],
        ],
        resize_keyboard=True,
    )


def channel_actions(channel_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = "Выключить" if is_active else "Включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"channel:toggle:{channel_id}",
                ),
                InlineKeyboardButton(
                    text="Удалить",
                    callback_data=f"channel:delete:{channel_id}",
                ),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="channel:list")],
        ]
    )


def telegram_accounts_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить аккаунт", callback_data="tg_account:add")],
        ]
    )


def telegram_account_actions(account_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = "Выключить" if is_active else "Включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"tg_account:toggle:{account_id}",
                ),
                InlineKeyboardButton(
                    text="Удалить",
                    callback_data=f"tg_account:delete:{account_id}",
                ),
            ],
        ]
    )
