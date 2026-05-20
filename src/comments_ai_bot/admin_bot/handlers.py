import logging
import re
import time

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from comments_ai_bot.admin_bot.keyboards import (
    ADD_CHANNEL,
    CANCEL,
    CHANNEL_LIST,
    HIGH_VIEW_POSTS,
    LOGS,
    SETTINGS,
    TELEGRAM_AUTH,
    cancel_keyboard,
    channel_actions,
    main_menu,
    telegram_account_actions,
    telegram_accounts_menu,
)
from comments_ai_bot.admin_bot.states import ChannelStates
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    LogRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.monitoring.manual_scan import ManualPostScanner
from comments_ai_bot.telegram_client.auth import remove_session_files, start_qr_login, wait_qr_login

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


@router.message(F.text == TELEGRAM_AUTH)
async def authorize_telegram_from_menu(message: Message) -> None:
    await send_telegram_accounts(message)


async def send_telegram_accounts(message: Message) -> None:
    async with async_session_factory() as session:
        accounts = await TelegramAccountRepository(session).list_all()

    await message.answer(
        f"Telegram-аккаунты: {len(accounts)}/100",
        reply_markup=telegram_accounts_menu(),
    )
    if not accounts:
        await message.answer("Аккаунты ещё не добавлены.")
        return

    for account in accounts:
        active = "активен" if account.is_active else "выключен"
        title = account.username or account.first_name or str(account.telegram_user_id or account.id)
        text = (
            f"#{account.id} {title}\n"
            f"Статус: {account.status}, {active}\n"
            f"Сессия: {account.session_name}"
        )
        if account.last_error:
            text = f"{text}\nОшибка: {account.last_error}"
        await message.answer(text, reply_markup=telegram_account_actions(account.id, account.is_active))


@router.callback_query(F.data == "tg_account:add")
async def add_telegram_account(callback: CallbackQuery) -> None:
    await callback.answer("Генерирую QR")
    await send_telegram_qr_auth(callback.message)


async def send_telegram_qr_auth(message: Message) -> None:
    try:
        async with async_session_factory() as session:
            repo = TelegramAccountRepository(session)
            session_name = f"tg_account_{int(time.time())}"
            account = await repo.create_pending(session_name)
            await session.commit()

        await message.answer("Генерирую QR-код для нового Telegram-аккаунта.")
        client, qr_login, qr_png = await start_qr_login(session_name)
    except Exception as error:
        logger.exception("Failed to start Telegram QR login")
        await message.answer(f"Не удалось создать QR-код: {error}", reply_markup=main_menu())
        return

    await message.answer_photo(
        BufferedInputFile(qr_png, filename="telegram-auth-qr.png"),
        caption=(
            "Открой Telegram на телефоне:\n"
            "Настройки -> Устройства -> Подключить устройство.\n\n"
            "Сканируй QR-код. Жду до 2 минут."
        ),
    )

    result = await wait_qr_login(client, qr_login)
    async with async_session_factory() as session:
        repo = TelegramAccountRepository(session)
        if result.ok:
            me = await start_authorized_account_probe(session_name)
            await repo.mark_authorized(
                account.id,
                telegram_user_id=me["id"],
                username=me["username"],
                first_name=me["first_name"],
                phone=me["phone"],
            )
            await LogRepository(session).info(
                "telegram_account_added",
                f"Добавлен Telegram-аккаунт {me['username'] or me['id']}",
                "telegram_account",
                account.id,
            )
        else:
            await repo.mark_error(account.id, result.message)
        await session.commit()

    await message.answer(result.message, reply_markup=main_menu())


async def start_authorized_account_probe(session_name: str) -> dict:
    from comments_ai_bot.telegram_client.auth import create_client

    client = await create_client(session_name)
    try:
        me = await client.get_me()
        return {
            "id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "phone": me.phone,
        }
    finally:
        await client.disconnect()


@router.callback_query(F.data.startswith("tg_account:toggle:"))
async def toggle_telegram_account(callback: CallbackQuery) -> None:
    account_id = int((callback.data or "").split(":")[-1])
    async with async_session_factory() as session:
        account = await TelegramAccountRepository(session).toggle(account_id)
        await session.commit()

    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    if account.status != "active":
        await callback.answer("Можно включать только авторизованный аккаунт", show_alert=True)
        return

    await callback.message.answer("Список аккаунтов обновлён.", reply_markup=main_menu())
    await send_telegram_accounts(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("tg_account:delete:"))
async def delete_telegram_account(callback: CallbackQuery) -> None:
    account_id = int((callback.data or "").split(":")[-1])
    async with async_session_factory() as session:
        account = await TelegramAccountRepository(session).delete(account_id)
        await session.commit()

    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return

    remove_session_files(account.session_name)
    await callback.message.answer("Telegram-аккаунт удалён.", reply_markup=main_menu())
    await send_telegram_accounts(callback.message)
    await callback.answer()


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
            (
                f"{channel}: проверено {stats['checked']}, "
                f"20к+ {stats['high_view']}, комменты открыты {stats['commentable']}"
            )
            for channel, stats in result.channel_stats.items()
        ]
        await message.answer("По каналам:\n" + "\n".join(stats_lines))

    if result.account_stats:
        account_lines = [
            f"{account}: каналов {count}"
            for account, count in result.account_stats.items()
        ]
        await message.answer("По аккаунтам:\n" + "\n".join(account_lines))

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
        comment_status = "комменты открыты" if post.comments_available else "комменты закрыты"
        line = (
            f"{post.channel_username} | {post.views_count} просмотров | {post.date}\n"
            f"{comment_status} | аккаунт: {post.account}\n"
            f"{post.url}"
        )
        if post.comments_reason and not post.comments_available:
            line = f"{line}\nПричина: {post.comments_reason}"
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
