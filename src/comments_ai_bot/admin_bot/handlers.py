import logging
import re
import time

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from comments_ai_bot.admin_bot.keyboards import (
    ADD_CHANNEL,
    AUTO_ADD_CHANNELS,
    CANCEL,
    CHANNEL_LIST,
    LOGS,
    READY_TO_COMMENT_POSTS,
    SETTINGS,
    TELEGRAM_AUTH,
    TEST_COMMENT,
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
from comments_ai_bot.discovery.tgstat import TgstatChannelImporter
from comments_ai_bot.monitoring.manual_scan import ManualPostScanner
from comments_ai_bot.publishing.test_comments import TestCommentSender
from comments_ai_bot.telegram_client.auth import remove_session_files, start_qr_login, wait_qr_login

router = Router()
logger = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
TELEGRAM_MESSAGE_LIMIT = 3500
READY_POSTS_LIMIT = 20
TEST_SENT_LIMIT = 30


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


@router.message(F.text == AUTO_ADD_CHANNELS)
async def auto_add_channels_from_tgstat(message: Message) -> None:
    await TgstatImportReporter(message).send()


async def send_channel_list(message: Message) -> None:
    async with async_session_factory() as session:
        channels = await ChannelRepository(session).list_all()

    if not channels:
        await message.answer("Каналы ещё не добавлены.", reply_markup=main_menu())
        return

    lines = [f"Каналов: {len(channels)}", ""]
    lines.extend(channel.username for channel in channels)

    for chunk in split_messages(lines):
        await message.answer(chunk, reply_markup=main_menu())


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


@router.message(F.text == READY_TO_COMMENT_POSTS)
async def ready_to_comment_posts_from_menu(message: Message) -> None:
    await ReadyPostsReporter(message).send()


@router.message(F.text == TEST_COMMENT)
async def send_test_comment_from_menu(message: Message) -> None:
    await TestCommentsReporter(message).send()


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
    await ReadyPostsReporter(callback.message).send()


class ReadyPostsReporter:
    def __init__(self, message: Message) -> None:
        self.message = message

    async def send(self) -> None:
        await self.message.answer("Сканирую каналы и ищу Ready 20к+.")

        try:
            result = await ManualPostScanner().scan_high_view_posts()
        except Exception as error:
            logger.exception("Ready posts scan button failed")
            async with async_session_factory() as session:
                await LogRepository(session).error(
                    "ready_posts_scan_failed",
                    str(error),
                    payload={"exception_type": type(error).__name__},
                )
                await session.commit()
            await self.message.answer(
                "Ошибка скана. Подробности записаны в logs/app.log.",
                reply_markup=main_menu(),
            )
            return

        ready_posts = [
            post
            for post in sorted(
                result.high_view_posts,
                key=lambda item: item.views_count,
                reverse=True,
            )
            if post.comments_available
        ]
        summary = (
            "Ready 20к+ готово.\n"
            f"Каналов: {result.channels_total}, ошибок: {result.channels_failed}\n"
            f"Проверено: {result.posts_checked}, 20к+: {len(result.high_view_posts)}, "
            f"ready: {len(ready_posts)}"
        )
        await self.message.answer(summary, reply_markup=main_menu())

        if result.errors:
            await self.message.answer("Ошибки: " + "; ".join(result.errors[:5]))

        if not ready_posts:
            return

        lines = [
            f"{post.channel_username} | {post.views_count} | {post.url}"
            for post in ready_posts[:READY_POSTS_LIMIT]
        ]
        if len(ready_posts) > READY_POSTS_LIMIT:
            lines.append(f"...ещё {len(ready_posts) - READY_POSTS_LIMIT}")

        for chunk in split_messages(lines):
            await self.message.answer(chunk)


class TestCommentsReporter:
    def __init__(self, message: Message) -> None:
        self.message = message

    async def send(self) -> None:
        await self.message.answer("Запускаю тестовую отправку.")

        result = await TestCommentSender().send_one_per_channel()
        sent_items = [item for item in result.items if item.status == "sent"]
        if result.errors and not sent_items:
            await self.message.answer(
                "Тест не выполнен: " + "; ".join(result.errors),
                reply_markup=main_menu(),
            )
            return

        summary = (
            "Тест завершён.\n"
            f"Проверенных отправок: {len(sent_items)}\n"
            f"Аккаунт: {result.account or '-'}"
        )
        if result.stopped_reason:
            summary = f"{summary}\nОстановка: {result.stopped_reason}"
        await self.message.answer(summary, reply_markup=main_menu())

        if not sent_items:
            return

        lines = [f"sent: {item.post_url}" for item in sent_items[:TEST_SENT_LIMIT]]
        if len(sent_items) > TEST_SENT_LIMIT:
            lines.append(f"...ещё {len(sent_items) - TEST_SENT_LIMIT}")

        for chunk in split_messages(lines):
            await self.message.answer(chunk)


class TgstatImportReporter:
    def __init__(self, message: Message) -> None:
        self.message = message

    async def send(self) -> None:
        await self.message.answer(
            "Ищу открытые каналы в TGStat "
            "и проверяю доступ через Telegram."
        )

        try:
            result = await TgstatChannelImporter().import_public_channels()
        except Exception as error:
            logger.exception("TGStat channel import failed")
            async with async_session_factory() as session:
                await LogRepository(session).error(
                    "tgstat_channels_import_failed",
                    str(error),
                    payload={"exception_type": type(error).__name__},
                )
                await session.commit()
            await self.message.answer(
                "Импорт TGStat не выполнен. "
                "Подробности записаны в logs/app.log.",
                reply_markup=main_menu(),
            )
            return

        summary = (
            "Импорт TGStat завершён.\n"
            f"Источников: {result.sources_checked}\n"
            f"Страниц: {result.pages_checked}\n"
            f"Найдено username: {result.candidates_found}\n"
            f"Добавлено: {result.channels_added}\n"
            f"Уже было: {result.channels_existing}\n"
            f"Пропущено: {result.channels_skipped}"
        )
        await self.message.answer(summary, reply_markup=main_menu())

        if result.errors:
            await self.message.answer("Ошибки: " + "; ".join(result.errors[:5]))

        if not result.added_usernames:
            return

        lines = result.added_usernames[:READY_POSTS_LIMIT]
        if len(result.added_usernames) > READY_POSTS_LIMIT:
            lines.append(f"...ещё {len(result.added_usernames) - READY_POSTS_LIMIT}")

        for chunk in split_messages(lines):
            await self.message.answer(chunk)


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
