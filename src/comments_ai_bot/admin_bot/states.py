from aiogram.fsm.state import State, StatesGroup


class ChannelStates(StatesGroup):
    waiting_for_username = State()


class TelegramAuthStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_phone_code = State()
    waiting_for_2fa_password = State()
