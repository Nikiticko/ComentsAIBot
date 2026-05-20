from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить канал", callback_data="channel:add")],
            [InlineKeyboardButton(text="Список каналов", callback_data="channel:list")],
            [InlineKeyboardButton(text="Логи", callback_data="logs:list")],
            [InlineKeyboardButton(text="Настройки", callback_data="settings:show")],
        ]
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
