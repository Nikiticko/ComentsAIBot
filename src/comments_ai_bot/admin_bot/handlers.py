import logging
import re

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from comments_ai_bot.admin_bot.keyboards import (
    ADD_CHANNEL,
    CANCEL,
    CHANNEL_LIST,
    HIGH_VIEW_POSTS,
    LOGS,
    SETTINGS,
    cancel_keyboard,
    channel_actions,
    main_menu,
)
from comments_ai_bot.admin_bot.states import ChannelStates
from comments_ai_bot.db.repositories import ChannelRepository, LogRepository
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.monitoring.manual_scan import ManualPostScanner

router = Router()
logger = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
TELEGRAM_MESSAGE_LIMIT = 3500


def normalize_channel_username(value: str) -> str | None:
    username = value.strip()
    username = username.removeprefix("https://t.me/").removeprefix("http://t.me/")
    username = username.removeprefix("t.me/")
    username = username.split("/", 1)[0].split("?", 1)[0]

    if not username.startswith("@"):
        username = f"@{username}"

    if not USERNAME_RE.fullmatch(username):
        return None
    return username


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Админка Comments AI Bot", reply_markup=main_menu())


@router.message(F.text == ADD_CHANNEL)
async def ask_channel_from_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(ChannelStates.waiting_for_username)
    await message.answer(
        "Отправь username публичного канала, например @channelname",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data == "channel:add")
async def ask_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChannelStates.waiting_for_username)
    await callback.message.answer(
        "Отправь username публичного канала, например @channelname",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(ChannelStates.waiting_for_username, F.text == CANCEL)
async def cancel_channel_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu())


@router.message(ChannelStates.waiting_for_username)
async def add_channel(message: Message, state: FSMContext) -> None:
    username = normalize_channel_username(message.text or "")
    if username is None:
        await message.answer(
            "Username канала не распознан. Нужен публичный канал в формате @channelname.",
            reply_markup=cancel_keyboard(),
        )
        return

    async with async_session_factory() as session:
        repo = ChannelRepository(session)
        existing = await repo.get_by_username(username)
        channel = await repo.add(username=username)
        event = "channel_enabled" if existing else "channel_added"
        message_text = f"Канал {username} включён повторно." if existing else f"Добавлен канал {username}"
        await LogRepository(session).info(event, message_text, "channel", channel.id)
        await session.commit()

    await state.clear()
    await message.answer(message_text, reply_markup=main_menu())


@router.callback_query(F.data == "common:cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Действие отменено.", reply_markup=main_menu())
    await callback.answer()


@router.message(F.text == CHANNEL_LIST)
async def list_channels_from_menu(message: Message) -> None:
    await send_channel_list(message)


@router.callback_query(F.data == "channel:list")
async def list_channels(callback: CallbackQuery) -> None:
    await send_channel_list(callback.message)
    await callback.answer()


async def send_channel_list(message: Message) -> None:
    async with async_session_factory() as session:
        channels = await ChannelRepository(session).list_all()

    if not channels:
        await message.answer("Каналы ещё не добавлены.", reply_markup=main_menu())
        return

    for channel in channels:
        status = "активен" if channel.is_active else "выключен"
        await message.answer(
            f"{channel.username}\nСтатус: {status}",
            reply_markup=channel_actions(channel.id, channel.is_active),
        )


@router.callback_query(F.data.startswith("channel:toggle:"))
async def toggle_channel(callback: CallbackQuery) -> None:
    channel_id = int((callback.data or "").split(":")[-1])
    async with async_session_factory() as session:
        repo = ChannelRepository(session)
        channel = await repo.toggle(channel_id)
        if channel is not None:
            status = "включён" if channel.is_active else "выключен"
            await LogRepository(session).info(
                "channel_toggled",
                f"Канал {channel.username} {status}",
                "channel",
                channel.id,
            )
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
    channel_id = int((callback.data or "").split(":")[-1])
    async with async_session_factory() as session:
        repo = ChannelRepository(session)
        channel = await repo.get(channel_id)
        deleted = await repo.delete(channel_id)
        if deleted and channel is not None:
            await LogRepository(session).info(
                "channel_deleted",
                f"Удалён канал {channel.username}",
                "channel",
                channel_id,
            )
        await session.commit()

    if not deleted:
        await callback.answer("Канал не найден", show_alert=True)
        return

    await callback.message.edit_text("Канал удалён.")
    await callback.message.answer("Главное меню", reply_markup=main_menu())
    await callback.answer()


@router.message(F.text == LOGS)
async def list_logs_from_menu(message: Message) -> None:
    await send_logs(message)


@router.callback_query(F.data == "logs:list")
async def list_logs(callback: CallbackQuery) -> None:
    await send_logs(callback.message)
    await callback.answer()


async def send_logs(message: Message) -> None:
    async with async_session_factory() as session:
        logs = await LogRepository(session).latest(limit=10)

    if not logs:
        await message.answer("Логов пока нет.", reply_markup=main_menu())
        return

    text = "\n\n".join(f"{log.created_at:%Y-%m-%d %H:%M} [{log.level}] {log.message}" for log in logs)
    await message.answer(text, reply_markup=main_menu())


@router.message(F.text == HIGH_VIEW_POSTS)
async def scan_high_view_posts_from_menu(message: Message) -> None:
    await send_high_view_posts_scan(message)


@router.callback_query(F.data == "posts:scan_high_views")
async def scan_high_view_posts(callback: CallbackQuery) -> None:
    await callback.answer("Парсинг запущен")
    await send_high_view_posts_scan(callback.message)


async def send_high_view_posts_scan(message: Message) -> None:
    await message.answer(
        "Начал парсить активные каналы за последние 24 часа. Это может занять немного времени."
    )

    try:
        result = await ManualPostScanner().scan_high_view_posts()
    except Exception as error:
        logger.exception("High-view post scan button failed")
        async with async_session_factory() as session:
            await LogRepository(session).error(
                "scan_button_failed",
                str(error),
                payload={"exception_type": type(error).__name__},
            )
            await session.commit()
        await message.answer("Ошибка парсинга. Подробности записаны в logs/app.log.", reply_markup=main_menu())
        return

    summary = (
        "Парсинг завершён.\n"
        f"Каналов: {result.channels_total}\n"
        f"Ошибок каналов: {result.channels_failed}\n"
        f"Период: последние {result.scan_hours} часа\n"
        f"Постов проверено: {result.posts_checked}\n"
        f"Постов сохранено: {result.posts_saved}\n"
        f"Постов 20к+: {len(result.high_view_posts)}"
    )
    await message.answer(summary, reply_markup=main_menu())

    if result.channel_stats:
        stats_lines = [
            f"{channel}: проверено {stats['checked']}, 20к+ {stats['high_view']}"
            for channel, stats in result.channel_stats.items()
        ]
        await message.answer("По каналам:\n" + "\n".join(stats_lines))

    if result.errors:
        error_lines = "\n".join(f"- {error}" for error in result.errors[:10])
        await message.answer(f"Ошибки парсинга:\n{error_lines}")

    if not result.high_view_posts:
        return

    sorted_posts = sorted(result.high_view_posts, key=lambda post: post.views_count, reverse=True)
    lines = []
    for post in sorted_posts:
        text_preview = (post.text or "").replace("\n", " ").strip()
        if len(text_preview) > 80:
            text_preview = f"{text_preview[:77]}..."
        line = f"{post.channel_username} | {post.views_count} просмотров | {post.date}\n{post.url}"
        if text_preview:
            line = f"{line}\n{text_preview}"
        lines.append(line)

    for chunk in split_messages(lines):
        await message.answer(chunk)


def split_messages(items: list[str], limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""

    for item in items:
        candidate = item if not current else f"{current}\n\n{item}"
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
        current = item

    if current:
        chunks.append(current)

    return chunks


@router.message(F.text == SETTINGS)
async def show_settings_from_menu(message: Message) -> None:
    await message.answer("Настройки будут добавлены на следующем этапе.", reply_markup=main_menu())


@router.callback_query(F.data == "settings:show")
async def show_settings(callback: CallbackQuery) -> None:
    await callback.message.answer("Настройки будут добавлены на следующем этапе.", reply_markup=main_menu())
    await callback.answer()
