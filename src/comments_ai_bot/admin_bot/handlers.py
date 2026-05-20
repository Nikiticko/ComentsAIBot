from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from comments_ai_bot.admin_bot.keyboards import channel_actions, main_menu
from comments_ai_bot.admin_bot.states import ChannelStates
from comments_ai_bot.db.repositories import ChannelRepository, LogRepository
from comments_ai_bot.db.session import async_session_factory

router = Router()


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Админка Comments AI Bot", reply_markup=main_menu())


@router.callback_query(F.data == "channel:add")
async def ask_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChannelStates.waiting_for_username)
    await callback.message.answer("Отправь username публичного канала, например @channelname")
    await callback.answer()


@router.message(ChannelStates.waiting_for_username)
async def add_channel(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("Username канала не распознан.")
        return

    username = username.removeprefix("https://t.me/").removeprefix("t.me/")
    if not username.startswith("@"):
        username = f"@{username}"

    async with async_session_factory() as session:
        repo = ChannelRepository(session)
        channel = await repo.add(username=username)
        await LogRepository(session).info("channel_added", f"Добавлен канал {username}", "channel", channel.id)
        await session.commit()

    await state.clear()
    await message.answer(f"Канал {username} добавлен.", reply_markup=main_menu())


@router.callback_query(F.data == "channel:list")
async def list_channels(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        channels = await ChannelRepository(session).list_all()

    if not channels:
        await callback.message.answer("Каналы ещё не добавлены.", reply_markup=main_menu())
        await callback.answer()
        return

    for channel in channels:
        status = "активен" if channel.is_active else "выключен"
        await callback.message.answer(
            f"{channel.username}\nСтатус: {status}",
            reply_markup=channel_actions(channel.id, channel.is_active),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("channel:toggle:"))
async def toggle_channel(callback: CallbackQuery) -> None:
    channel_id = int(callback.data.split(":")[-1])
    async with async_session_factory() as session:
        repo = ChannelRepository(session)
        channel = await repo.toggle(channel_id)
        await session.commit()

    if channel is None:
        await callback.answer("Канал не найден", show_alert=True)
        return

    status = "включен" if channel.is_active else "выключен"
    await callback.message.edit_text(
        f"{channel.username}\nСтатус: {status}",
        reply_markup=channel_actions(channel.id, channel.is_active),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("channel:delete:"))
async def delete_channel(callback: CallbackQuery) -> None:
    channel_id = int(callback.data.split(":")[-1])
    async with async_session_factory() as session:
        deleted = await ChannelRepository(session).delete(channel_id)
        await session.commit()

    if not deleted:
        await callback.answer("Канал не найден", show_alert=True)
        return

    await callback.message.edit_text("Канал удалён.")
    await callback.answer()


@router.callback_query(F.data == "logs:list")
async def list_logs(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        logs = await LogRepository(session).latest(limit=10)

    if not logs:
        await callback.message.answer("Логов пока нет.", reply_markup=main_menu())
        await callback.answer()
        return

    text = "\n\n".join(f"{log.created_at:%Y-%m-%d %H:%M} [{log.level}] {log.message}" for log in logs)
    await callback.message.answer(text, reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "settings:show")
async def show_settings(callback: CallbackQuery) -> None:
    await callback.message.answer("Настройки будут добавлены на следующем этапе.", reply_markup=main_menu())
    await callback.answer()
