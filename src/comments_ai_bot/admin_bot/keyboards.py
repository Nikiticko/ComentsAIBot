from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


ADD_CHANNEL = "Добавить канал"
AUTO_ADD_CHANNELS = "Авто каналы TGStat"
CHANNEL_LIST = "Список каналов"
READY_TO_COMMENT_POSTS = "Ready 20к+"
ONE_AI_SEND = "Одна ИИ-отправка"
AI_TEST = "Тест ИИ"
START_MAILING = "Начать рассылку"
STOP_MAILING = "Остановить рассылку"
TELEGRAM_AUTH = "Аккаунты TG"
LOGS = "Логи"
CANCEL = "Отмена"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADD_CHANNEL), KeyboardButton(text=CHANNEL_LIST)],
            [KeyboardButton(text=AUTO_ADD_CHANNELS)],
            [KeyboardButton(text=READY_TO_COMMENT_POSTS), KeyboardButton(text=ONE_AI_SEND)],
            [KeyboardButton(text=AI_TEST)],
            [KeyboardButton(text=START_MAILING), KeyboardButton(text=STOP_MAILING)],
            [KeyboardButton(text=TELEGRAM_AUTH), KeyboardButton(text=LOGS)],
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
            [
                InlineKeyboardButton(
                    text="Добавить через QR",
                    callback_data="tg_account:add_qr",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Добавить по номеру",
                    callback_data="tg_account:add_phone",
                )
            ],
            [InlineKeyboardButton(text="Подхватить текущую сессию", callback_data="tg_account:import_legacy")],
        ]
    )


def telegram_account_actions(
    account_id: int,
    is_active: bool,
    is_mailing_enabled: bool,
) -> InlineKeyboardMarkup:
    toggle_text = "Выключить" if is_active else "Включить"
    mailing_text = "Убрать из рассылки" if is_mailing_enabled else "Добавить в рассылку"
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
            [
                InlineKeyboardButton(
                    text=mailing_text,
                    callback_data=f"tg_account:toggle_mailing:{account_id}",
                )
            ],
        ]
    )
