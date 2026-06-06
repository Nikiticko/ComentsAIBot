from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
import time

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from comments_ai_bot.admin_bot.keyboards import (
    ADD_CHANNEL,
    AI_TEST,
    AUTO_ADD_CHANNELS,
    CANCEL,
    CHANNEL_LIST,
    LOGS,
    ONE_AI_SEND,
    READY_TO_COMMENT_POSTS,
    START_MAILING,
    STOP_MAILING,
    TELEGRAM_AUTH,
    cancel_keyboard,
    channel_actions,
    main_menu,
    telegram_account_actions,
    telegram_accounts_menu,
)
from comments_ai_bot.ai.service import MIN_AI_CONTEXT_TEXT_CHARS
from comments_ai_bot.ai.topic_test import AiTopicTester, MIN_AI_TEST_TEXT_CHARS
from comments_ai_bot.admin_bot.states import ChannelStates, TelegramAuthStates
from comments_ai_bot.core.config import settings
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    LogRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.discovery.tgstat import TgstatChannelImporter
from comments_ai_bot.monitoring.manual_scan import ManualPostScanner
from comments_ai_bot.publishing.mailing import mailing_automation
from comments_ai_bot.publishing.ai_comments import AiCommentSender
from comments_ai_bot.telegram_client.auth import (
    copy_legacy_session_files,
    create_legacy_client,
    finish_password_login,
    legacy_session_path,
    remove_session_files,
    start_qr_login,
    wait_qr_login,
)

router = Router()
logger = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
TELEGRAM_MESSAGE_LIMIT = 3500
READY_POSTS_LIMIT = 20
AI_SEND_LIMIT = 30


@dataclass
class PendingTelegram2FA:
    account_id: int
    session_name: str
    client: object


pending_telegram_2fa: dict[int, PendingTelegram2FA] = {}


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


@router.message(F.text == ONE_AI_SEND)
async def send_one_ai_comment_from_menu(message: Message) -> None:
    await OneAiSendReporter(message).send()


@router.message(F.text == AI_TEST)
async def test_ai_topic_from_menu(message: Message) -> None:
    await AiTopicTestReporter(message).send()


@router.message(F.text == START_MAILING)
async def start_mailing_from_menu(message: Message) -> None:
    if message.bot is None:
        await message.answer("Не удалось получить bot instance.", reply_markup=main_menu())
        return

    started = await mailing_automation.start(message.bot, message.chat.id)
    if not started:
        await message.answer("Рассылка уже запущена.", reply_markup=main_menu())
        return

    await message.answer(
        (
            "Рассылка запущена. Каждые 30 секунд каждый активный TG-аккаунт "
            "делает до одной отправки в канал, где сегодня ещё не было "
            "успешного комментария.\n"
            f"Процедура: текст от {MIN_AI_CONTEXT_TEXT_CHARS} символов, "
            "открытые комментарии, анализ темы ИИ, генерация комментария, "
            "проверка комментария ИИ, публикация."
        ),
        reply_markup=main_menu(),
    )


@router.message(F.text == STOP_MAILING)
async def stop_mailing_from_menu(message: Message) -> None:
    stopped = await mailing_automation.stop()
    text = "Рассылка остановлена." if stopped else "Рассылка не запущена."
    await message.answer(text, reply_markup=main_menu())


@router.message(F.text == TELEGRAM_AUTH)
async def authorize_telegram_from_menu(message: Message) -> None:
    await send_telegram_accounts(message)


async def send_telegram_accounts(message: Message) -> None:
    async with async_session_factory() as session:
        accounts = await TelegramAccountRepository(session).list_all()

    legacy_info = await get_legacy_account_info(accounts)
    total_accounts = len(accounts) + (1 if legacy_info else 0)
    await message.answer(
        f"Telegram-аккаунты: {total_accounts}/100",
        reply_markup=telegram_accounts_menu(),
    )
    if not accounts and legacy_info is None:
        await message.answer("Аккаунты ещё не добавлены.")
        return

    if legacy_info is not None:
        await message.answer(
            (
                f"Legacy: {legacy_info['title']}\n"
                "Статус: active, активен\n"
                f"Сессия: {legacy_info['session_name']}\n"
                "Источник: data/*.session"
            )
        )

    for account in accounts:
        active = "активен" if account.is_active else "выключен"
        title = account.username or account.first_name or str(account.telegram_user_id or account.id)
        text = (
            f"#{account.id} {title}\n"
            f"Статус: {account.status}, {active}\n"
            f"Сессия: {account.session_name}"
        )
        cooldown_text = format_account_cooldown(account)
        if cooldown_text:
            text = f"{text}\n{cooldown_text}"
        if account.last_error and not cooldown_text:
            text = f"{text}\nОшибка: {account.last_error}"
        await message.answer(text, reply_markup=telegram_account_actions(account.id, account.is_active))


def format_account_cooldown(account) -> str | None:
    if account.cooldown_until is None:
        return None

    cooldown_until = as_utc(account.cooldown_until)
    now = datetime.now(timezone.utc)
    if cooldown_until <= now:
        return None

    seconds_left = int((cooldown_until - now).total_seconds())
    hours_left = max(1, seconds_left // 3600)
    reason = account.cooldown_reason or account.cooldown_source or "Telegram cooldown"
    return (
        f"Пауза до: {cooldown_until:%Y-%m-%d %H:%M UTC}\n"
        f"Осталось: ~{hours_left} ч\n"
        f"Причина паузы: {reason}"
    )


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def get_legacy_account_info(existing_accounts) -> dict | None:
    session_name = settings.telegram_session_name
    if not legacy_session_path(session_name).with_suffix(".session").exists():
        return None
    if any(account.session_name == session_name for account in existing_accounts):
        return None

    try:
        me = await probe_legacy_account(session_name)
    except Exception as error:
        logger.warning("Failed to inspect legacy Telegram session: %s", error)
        return {
            "session_name": session_name,
            "title": session_name,
        }

    return {
        "session_name": session_name,
        "title": me["username"] or me["first_name"] or str(me["id"]),
        **me,
    }


@router.callback_query(F.data == "tg_account:add")
async def add_telegram_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Генерирую QR")
    await send_telegram_qr_auth(callback.message, state, callback.from_user.id)


async def send_telegram_qr_auth(message: Message, state: FSMContext, user_id: int) -> None:
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

    result = await wait_qr_login(client, qr_login, session_name=session_name)
    if result.needs_password:
        old_pending = pending_telegram_2fa.pop(user_id, None)
        if old_pending is not None:
            await old_pending.client.disconnect()

        pending_telegram_2fa[user_id] = PendingTelegram2FA(
            account_id=account.id,
            session_name=session_name,
            client=client,
        )
        await state.set_state(TelegramAuthStates.waiting_for_2fa_password)
        await message.answer(result.message, reply_markup=cancel_keyboard())
        return

    async with async_session_factory() as session:
        repo = TelegramAccountRepository(session)
        if result.ok:
            await save_authorized_telegram_account(repo, session, account.id, session_name)
        else:
            await repo.mark_error(account.id, result.message)
        await session.commit()

    await message.answer(result.message, reply_markup=main_menu())


@router.message(TelegramAuthStates.waiting_for_2fa_password, F.text == CANCEL)
async def cancel_telegram_2fa(message: Message, state: FSMContext) -> None:
    pending = pending_telegram_2fa.pop(message.from_user.id, None)
    if pending is not None:
        await pending.client.disconnect()
        async with async_session_factory() as session:
            await TelegramAccountRepository(session).mark_error(
                pending.account_id,
                "Ввод 2FA-пароля отменён.",
            )
            await session.commit()

    await state.clear()
    await message.answer("Авторизация Telegram отменена.", reply_markup=main_menu())


@router.message(TelegramAuthStates.waiting_for_2fa_password)
async def finish_telegram_2fa(message: Message, state: FSMContext) -> None:
    pending = pending_telegram_2fa.get(message.from_user.id)
    if pending is None:
        await state.clear()
        await message.answer(
            "Активная авторизация не найдена. Запусти добавление аккаунта снова.",
            reply_markup=main_menu(),
        )
        return

    password = (message.text or "").strip()
    if not password:
        await message.answer(
            "Отправь 2FA-пароль Telegram или нажми Отмена.",
            reply_markup=cancel_keyboard(),
        )
        return

    try:
        await message.delete()
    except Exception:
        logger.warning("Failed to delete Telegram 2FA password message")

    result = await finish_password_login(
        pending.client,
        password,
        session_name=pending.session_name,
    )
    if result.needs_password:
        await message.answer(result.message, reply_markup=cancel_keyboard())
        return

    pending_telegram_2fa.pop(message.from_user.id, None)
    try:
        async with async_session_factory() as session:
            repo = TelegramAccountRepository(session)
            if result.ok:
                me = await connected_account_info(pending.client)
                await save_authorized_telegram_account(
                    repo,
                    session,
                    pending.account_id,
                    pending.session_name,
                    me=me,
                )
            else:
                await repo.mark_error(pending.account_id, result.message)
            await session.commit()
    finally:
        await pending.client.disconnect()

    await state.clear()
    await message.answer(result.message, reply_markup=main_menu())


async def save_authorized_telegram_account(
    repo: TelegramAccountRepository,
    session,
    account_id: int,
    session_name: str,
    *,
    me: dict | None = None,
) -> None:
    if me is None:
        me = await start_authorized_account_probe(session_name)

    await repo.mark_authorized(
        account_id,
        telegram_user_id=me["id"],
        username=me["username"],
        first_name=me["first_name"],
        phone=me["phone"],
    )
    await LogRepository(session).info(
        "telegram_account_added",
        f"Добавлен Telegram-аккаунт {me['username'] or me['id']}",
        "telegram_account",
        account_id,
    )


async def connected_account_info(client: object) -> dict:
    me = await client.get_me()
    return {
        "id": me.id,
        "username": me.username,
        "first_name": me.first_name,
        "phone": me.phone,
    }


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


async def probe_legacy_account(session_name: str) -> dict:
    client = await create_legacy_client(session_name)
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("legacy-сессия не авторизована")

        me = await client.get_me()
        if me.bot:
            raise RuntimeError("legacy-сессия авторизована как бот")

        return {
            "id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "phone": me.phone,
        }
    finally:
        await client.disconnect()


@router.callback_query(F.data == "tg_account:import_legacy")
async def import_legacy_telegram_account(callback: CallbackQuery) -> None:
    session_name = settings.telegram_session_name
    if not legacy_session_path(session_name).with_suffix(".session").exists():
        await callback.answer("Legacy-сессия не найдена", show_alert=True)
        return

    try:
        me = await probe_legacy_account(session_name)
    except Exception as error:
        logger.exception("Failed to import legacy Telegram session")
        await callback.answer("Legacy-сессия не авторизована", show_alert=True)
        await callback.message.answer(f"Не удалось подхватить сессию: {error}")
        return

    copy_legacy_session_files(session_name)
    async with async_session_factory() as session:
        repo = TelegramAccountRepository(session)
        account = await repo.upsert_authorized(
            session_name,
            telegram_user_id=me["id"],
            username=me["username"],
            first_name=me["first_name"],
            phone=me["phone"],
        )
        await LogRepository(session).info(
            "telegram_legacy_account_imported",
            f"Подхвачена Telegram-сессия {me['username'] or me['id']}",
            "telegram_account",
            account.id,
        )
        await session.commit()

    await callback.message.answer("Текущая Telegram-сессия добавлена в аккаунты.")
    await send_telegram_accounts(callback.message)
    await callback.answer()


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


class OneAiSendReporter:
    def __init__(self, message: Message) -> None:
        self.message = message

    async def send(self) -> None:
        await self.message.answer("Запускаю одну ИИ-отправку.")

        result = await AiCommentSender().send_one_per_channel()
        sent_items = [item for item in result.items if item.status == "sent"]
        if result.errors and not sent_items:
            await self.message.answer(
                "ИИ-отправка не выполнена: " + "; ".join(result.errors),
                reply_markup=main_menu(),
            )
            return

        summary = (
            "ИИ-отправка завершена.\n"
            f"Отправлено: {len(sent_items)}\n"
            f"Постов проверено: {result.posts_checked}\n"
            f"Дошло до ИИ: {result.posts_reached_ai}\n"
            f"Пропущено: {result.comments_skipped}\n"
            f"Ошибок: {result.comments_failed}\n"
            f"Аккаунт: {result.account or '-'}"
        )
        if result.stopped_reason:
            summary = f"{summary}\nОстановка: {result.stopped_reason}"
        await self.message.answer(summary, reply_markup=main_menu())

        if not sent_items:
            return

        lines = [f"sent: {item.post_url}" for item in sent_items[:AI_SEND_LIMIT]]
        if len(sent_items) > AI_SEND_LIMIT:
            lines.append(f"...ещё {len(sent_items) - AI_SEND_LIMIT}")

        for chunk in split_messages(lines):
            await self.message.answer(chunk)


class AiTopicTestReporter:
    def __init__(self, message: Message) -> None:
        self.message = message

    async def send(self) -> None:
        await self.message.answer(
            "Ищу случайный пост с открытыми комментариями и текстом от "
            f"{MIN_AI_TEST_TEXT_CHARS} символов."
        )

        try:
            result = await AiTopicTester().analyze_random_commentable_post()
        except Exception as error:
            logger.exception("AI topic test button failed")
            async with async_session_factory() as session:
                await LogRepository(session).error(
                    "ai_topic_test_failed",
                    str(error),
                    payload={"exception_type": type(error).__name__},
                )
                await session.commit()
            await self.message.answer(
                "Тест ИИ не выполнен. Подробности записаны в logs/app.log.",
                reply_markup=main_menu(),
            )
            return

        if result.post is None:
            reason = "; ".join(result.errors[:5]) or "подходящий пост не найден"
            await self.message.answer(
                (
                    "Тест ИИ не выполнен.\n"
                    f"Каналов: {result.channels_total}, проверено: {result.channels_attempted}\n"
                    f"Постов проверено: {result.posts_checked}\n"
                    f"Дошло до ИИ: {result.posts_reached_ai}\n"
                    f"Без текста: {result.posts_without_text}\n"
                    f"Короткий текст до {MIN_AI_TEST_TEXT_CHARS}: {result.posts_too_short}\n"
                    f"Комментарии закрыты: {result.posts_comments_closed}\n"
                    f"Битых каналов отключено: {result.broken_channels}\n"
                    f"Причина: {reason}"
                ),
                reply_markup=main_menu(),
            )
            return

        validation = result.post.validation
        confidence_text = "-" if validation.confidence is None else str(validation.confidence)
        reason = validation.reason or "-"
        matched = validation.trigger_word or validation.matched_topic or "-"
        status = "прошёл" if validation.passed else "не прошёл"
        ai_used = "да" if validation.ai_used else "нет"
        text_preview = crop_text(result.post.text, 1_500)
        comment_validation = result.post.comment_validation or {}
        comment_status = "-"
        if comment_validation:
            comment_status = (
                "прошёл" if comment_validation.get("allowed") else "не прошёл"
            )
        comment_reason = comment_validation.get("reason") or "-"
        generated_comment = result.post.generated_comment or "-"
        answer = (
            "Тест валидации готов.\n"
            f"Канал: {result.post.channel_username}\n"
            f"Пост: {result.post.url}\n"
            f"Просмотры: {result.post.views_count or 0}\n"
            f"Аккаунт: {result.account or '-'}\n\n"
            "Статистика теста:\n"
            f"Каналов проверено: {result.channels_attempted}\n"
            f"Постов проверено: {result.posts_checked}\n"
            f"Дошло до ИИ: {result.posts_reached_ai}\n"
            f"Без текста: {result.posts_without_text}\n"
            f"Коротких: {result.posts_too_short}\n"
            f"Комментарии закрыты: {result.posts_comments_closed}\n"
            f"Битых каналов отключено: {result.broken_channels}\n\n"
            f"Статус: {status}\n"
            f"Уровень: {validation.level}\n"
            f"ИИ работал: {ai_used}\n"
            f"Совпадение: {matched}\n"
            f"Тема: {validation.topic or '-'}\n"
            f"Уверенность: {confidence_text}\n"
            f"Причина: {reason}\n\n"
            "Dry-run комментария:\n"
            f"{generated_comment}\n"
            f"Проверка комментария: {comment_status}\n"
            f"Причина проверки: {comment_reason}\n\n"
            f"Текст поста:\n{text_preview}"
        )
        await self.message.answer(answer, reply_markup=main_menu())


class TgstatImportReporter:
    def __init__(self, message: Message) -> None:
        self.message = message

    async def send(self) -> None:
        await self.message.answer(
            "Добираю базу каналов из TGStat до целевого объёма."
        )

        try:
            result = await TgstatChannelImporter(target_total=10_000).import_public_channels()
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
            f"Цель в базе: {result.target_total}\n"
            f"Было каналов: {result.channels_total_before}\n"
            f"Стало каналов: {result.channels_total_after}\n"
            f"Источников: {result.sources_checked}\n"
            f"Страниц: {result.pages_checked}, "
            f"ошибок страниц: {result.pages_failed}\n"
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


def crop_text(text: str, limit: int) -> str:
    clean_text = text.strip()
    if len(clean_text) <= limit:
        return clean_text
    return f"{clean_text[: limit - 3]}..."
