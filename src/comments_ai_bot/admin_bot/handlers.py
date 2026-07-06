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
    AUTO_ADD_CHANNELS,
    BAD_CHANNELS,
    CANCEL,
    CHANNEL_STATS,
    CHANNEL_LIST,
    LOGS,
    START_MAILING,
    STOP_MAILING,
    TELEGRAM_AUTH,
    cancel_keyboard,
    channel_actions,
    logs_actions,
    main_menu,
    telegram_account_actions,
    telegram_accounts_menu,
)
from comments_ai_bot.ai.service import MIN_AI_CONTEXT_TEXT_CHARS
from comments_ai_bot.admin_bot.states import ChannelStates, TelegramAuthStates
from comments_ai_bot.core.config import settings
from comments_ai_bot.core.types import ChannelStatus
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    LogRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.discovery.israel import IsraelChannelDiscoverer
from comments_ai_bot.publishing.mailing import mailing_automation
from comments_ai_bot.telegram_client.auth import (
    copy_legacy_session_files,
    create_legacy_client,
    finish_phone_code_login,
    finish_password_login,
    legacy_session_path,
    remove_session_files,
    start_phone_login,
    start_qr_login,
    wait_qr_login,
)

router = Router()
logger = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
TELEGRAM_MESSAGE_LIMIT = 3500
TGSTAT_PREVIEW_LIMIT = 20


@dataclass
class PendingTelegramAuth:
    account_id: int
    session_name: str
    client: object
    phone: str | None = None


pending_telegram_auth: dict[int, PendingTelegramAuth] = {}


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


def channel_efficiency_score(channel) -> float:
    return channel.success_count / max(channel.posts_checked_count, 1)


def format_channel_line(channel, *, with_error: bool = False) -> str:
    score = channel_efficiency_score(channel)
    line = (
        f"{channel.username} | {channel.status} | "
        f"ok {channel.success_count}/{channel.checks_count} | "
        f"posts {channel.posts_checked_count} | score {score:.3f}"
    )
    if with_error and channel.last_error:
        line = f"{line} | {channel.last_error[:160]}"
    return line


def format_channel_score(channel) -> str:
    return (
        f"{channel.username}: {channel_efficiency_score(channel):.3f} "
        f"({channel.success_count}/{channel.posts_checked_count}, {channel.status})"
    )


def new_telegram_session_name() -> str:
    return f"tg_account_{time.time_ns() // 1_000_000}"


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
        channel = await repo.add(username=username, activate_existing=existing is not None)
        event = "channel_enabled" if existing else "channel_added"
        message_text = (
            f"Канал {username} включён повторно."
            if existing
            else f"Добавлен канал {username}"
        )
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
    await IsraelDiscoveryReporter(message).send()


async def send_channel_list(message: Message) -> None:
    async with async_session_factory() as session:
        channels = await ChannelRepository(session).list_all()

    if not channels:
        await message.answer("Каналы ещё не добавлены.", reply_markup=main_menu())
        return

    lines = [f"Каналов: {len(channels)}", ""]
    lines.extend(format_channel_line(channel) for channel in channels)

    for chunk in split_messages(lines):
        await message.answer(chunk, reply_markup=main_menu())


@router.message(F.text == BAD_CHANNELS)
async def bad_channels_from_menu(message: Message) -> None:
    async with async_session_factory() as session:
        channels = await ChannelRepository(session).list_all()

    bad_statuses = {
        ChannelStatus.BAD_USERNAME.value,
        ChannelStatus.WRITE_FORBIDDEN.value,
        ChannelStatus.NEED_JOIN.value,
        ChannelStatus.LOW_EFFICIENCY.value,
        ChannelStatus.IGNORED.value,
        ChannelStatus.COMMENTS_CLOSED.value,
    }
    bad_channels = [channel for channel in channels if channel.status in bad_statuses]
    if not bad_channels:
        await message.answer("Плохих каналов нет.", reply_markup=main_menu())
        return

    lines = [f"Плохих каналов: {len(bad_channels)}", ""]
    lines.extend(format_channel_line(channel, with_error=True) for channel in bad_channels)
    for chunk in split_messages(lines):
        await message.answer(chunk, reply_markup=main_menu())


@router.message(F.text == CHANNEL_STATS)
async def channel_stats_from_menu(message: Message) -> None:
    async with async_session_factory() as session:
        channels = await ChannelRepository(session).list_all()

    if not channels:
        await message.answer("Каналы ещё не добавлены.", reply_markup=main_menu())
        return

    top = sorted(channels, key=channel_efficiency_score, reverse=True)[:10]
    bottom = sorted(channels, key=channel_efficiency_score)[:10]
    lines = ["Самые эффективные:", *[format_channel_score(channel) for channel in top]]
    lines.extend(
        ["", "Самые бесполезные:", *[format_channel_score(channel) for channel in bottom]]
    )
    for chunk in split_messages(lines):
        await message.answer(chunk, reply_markup=main_menu())


@router.callback_query(F.data.startswith("channel:toggle:"))
async def toggle_channel(callback: CallbackQuery) -> None:
    channel_id = int((callback.data or "").split(":")[-1])
    async with async_session_factory() as session:
        repo = ChannelRepository(session)
        channel = await repo.toggle(channel_id)
        if channel is not None:
            status = channel.status
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

    await callback.message.edit_text(
        format_channel_line(channel, with_error=True),
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
        logs = await LogRepository(session).list_all()

    if not logs:
        await message.answer("Сохранённых логов пока нет.", reply_markup=logs_actions())
        return

    report = build_logs_report(logs)
    await message.answer_document(
        BufferedInputFile(report.encode("utf-8"), filename="comments-ai-bot-logs.txt"),
        caption=f"Сохранённых логов: {len(logs)}",
        reply_markup=logs_actions(),
    )


@router.callback_query(F.data == "logs:clear")
async def clear_logs(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        deleted_count = await LogRepository(session).delete_all()
        await session.commit()

    await callback.message.answer(
        f"Логи очищены. Удалено записей: {deleted_count}.",
        reply_markup=main_menu(),
    )
    await callback.answer()


def build_logs_report(logs) -> str:
    lines = ["Comments AI Bot logs", f"Total: {len(logs)}", ""]
    for log in logs:
        entity = ""
        if log.entity_type or log.entity_id is not None:
            entity = f" | entity={log.entity_type or '-'}:{log.entity_id or '-'}"
        payload = f" | payload={log.payload}" if log.payload else ""
        lines.append(
            (
                f"{log.created_at:%Y-%m-%d %H:%M:%S} "
                f"[{log.level}] {log.event}{entity}{payload}\n"
                f"{log.message}"
            )
        )
        lines.append("")

    return "\n".join(lines)


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
            "Рассылка запущена. Доступные TG-аккаунты делают до одной отправки "
            "в канал, где сегодня ещё не было успешного комментария.\n"
            f"Интервал цикла: {settings.mailing_interval_seconds} сек. "
            f"Отдых аккаунта: {settings.telegram_account_min_idle_seconds} сек.\n"
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
                "Рассылка: нет\n"
                f"Сессия: {legacy_info['session_name']}\n"
                "Источник: data/*.session"
            )
        )

    for account in accounts:
        active = "активен" if account.is_active else "выключен"
        effective_mailing = is_effective_mailing_account(account)
        mailing = "да" if effective_mailing else "нет"
        if account.telegram_user_id in settings.admin_ids:
            mailing = f"{mailing} (админский)"
        title = account.username or account.first_name or str(account.telegram_user_id or account.id)
        text = (
            f"#{account.id} {title}\n"
            f"Статус: {account.status}, {active}\n"
            f"Рассылка: {mailing}\n"
            f"Сессия: {account.session_name}"
        )
        cooldown_text = format_account_cooldown(account)
        if cooldown_text:
            text = f"{text}\n{cooldown_text}"
        if account.last_error and not cooldown_text:
            text = f"{text}\nОшибка: {account.last_error}"
        await message.answer(
            text,
            reply_markup=telegram_account_actions(
                account.id,
                account.is_active,
                effective_mailing,
            ),
        )


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


def is_effective_mailing_account(account) -> bool:
    return account.is_mailing_enabled and account.telegram_user_id not in settings.admin_ids


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


@router.callback_query(F.data == "tg_account:add_qr")
async def add_telegram_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Генерирую QR")
    await send_telegram_qr_auth(callback.message, state, callback.from_user.id)


@router.callback_query(F.data == "tg_account:add_phone")
async def ask_telegram_phone(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TelegramAuthStates.waiting_for_phone)
    await callback.message.answer(
        "Отправь номер рассылочного Telegram-аккаунта, например +380XXXXXXXXX.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(TelegramAuthStates.waiting_for_phone, F.text == CANCEL)
async def cancel_telegram_phone_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Авторизация Telegram отменена.", reply_markup=main_menu())


@router.message(TelegramAuthStates.waiting_for_phone)
async def start_telegram_phone_auth(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    session_name = new_telegram_session_name()

    async with async_session_factory() as session:
        repo = TelegramAccountRepository(session)
        account = await repo.create_pending(session_name)
        await session.commit()

    try:
        client, result = await start_phone_login(phone, session_name)
    except Exception as error:
        logger.exception("Failed to start Telegram phone login")
        result_message = f"Не удалось запросить код Telegram: {error}"
        remove_session_files(session_name)
        async with async_session_factory() as session:
            await TelegramAccountRepository(session).mark_error(account.id, result_message)
            await session.commit()
        await state.clear()
        await message.answer(result_message, reply_markup=main_menu())
        return

    if client is None:
        remove_session_files(session_name)
        async with async_session_factory() as session:
            await TelegramAccountRepository(session).mark_error(account.id, result.message)
            await session.commit()
        await state.clear()
        await message.answer(result.message, reply_markup=main_menu())
        return

    old_pending = pending_telegram_auth.pop(message.from_user.id, None)
    if old_pending is not None:
        await old_pending.client.disconnect()

    pending_telegram_auth[message.from_user.id] = PendingTelegramAuth(
        account_id=account.id,
        session_name=session_name,
        client=client,
        phone=phone,
    )
    await state.set_state(TelegramAuthStates.waiting_for_phone_code)
    await message.answer(
        (
            f"{result.message}\n"
            "Код ищи в приложении Telegram на этом аккаунте, не в админ-боте."
        ),
        reply_markup=cancel_keyboard(),
    )


@router.message(TelegramAuthStates.waiting_for_phone_code, F.text == CANCEL)
async def cancel_telegram_phone_code(message: Message, state: FSMContext) -> None:
    await cancel_pending_telegram_auth(message, state)


@router.message(TelegramAuthStates.waiting_for_phone_code)
async def finish_telegram_phone_code(message: Message, state: FSMContext) -> None:
    pending = pending_telegram_auth.get(message.from_user.id)
    if pending is None or pending.phone is None:
        await state.clear()
        await message.answer(
            "Активная авторизация не найдена. Запусти добавление аккаунта снова.",
            reply_markup=main_menu(),
        )
        return

    code = (message.text or "").strip()
    if not code:
        await message.answer("Отправь код Telegram или нажми Отмена.", reply_markup=cancel_keyboard())
        return

    result = await finish_phone_code_login(
        pending.client,
        pending.phone,
        code,
        session_name=pending.session_name,
    )
    if result.needs_password:
        await state.set_state(TelegramAuthStates.waiting_for_2fa_password)
        await message.answer(result.message, reply_markup=cancel_keyboard())
        return
    if result.needs_code:
        await message.answer(result.message, reply_markup=cancel_keyboard())
        return

    await complete_pending_telegram_auth(message, state, pending, result)


async def send_telegram_qr_auth(message: Message, state: FSMContext, user_id: int) -> None:
    try:
        async with async_session_factory() as session:
            repo = TelegramAccountRepository(session)
            session_name = new_telegram_session_name()
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
        old_pending = pending_telegram_auth.pop(user_id, None)
        if old_pending is not None:
            await old_pending.client.disconnect()

        pending_telegram_auth[user_id] = PendingTelegramAuth(
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
    await cancel_pending_telegram_auth(message, state)


async def cancel_pending_telegram_auth(message: Message, state: FSMContext) -> None:
    pending = pending_telegram_auth.pop(message.from_user.id, None)
    if pending is not None:
        await pending.client.disconnect()
        remove_session_files(pending.session_name)
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
    pending = pending_telegram_auth.get(message.from_user.id)
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

    await complete_pending_telegram_auth(message, state, pending, result)


async def complete_pending_telegram_auth(
    message: Message,
    state: FSMContext,
    pending: PendingTelegramAuth,
    result,
) -> None:
    pending_telegram_auth.pop(message.from_user.id, None)
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
                remove_session_files(pending.session_name)
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
    is_mailing_enabled: bool = True,
) -> None:
    if me is None:
        me = await start_authorized_account_probe(session_name)

    if me["id"] in settings.admin_ids:
        is_mailing_enabled = False

    await repo.mark_authorized(
        account_id,
        telegram_user_id=me["id"],
        username=me["username"],
        first_name=me["first_name"],
        phone=me["phone"],
        is_mailing_enabled=is_mailing_enabled,
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
            is_mailing_enabled=False,
        )
        await LogRepository(session).info(
            "telegram_legacy_account_imported",
            f"Подхвачена Telegram-сессия {me['username'] or me['id']}",
            "telegram_account",
            account.id,
        )
        await session.commit()

    await callback.message.answer(
        "Текущая Telegram-сессия добавлена в аккаунты без участия в рассылке."
    )
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


@router.callback_query(F.data.startswith("tg_account:toggle_mailing:"))
async def toggle_telegram_account_mailing(callback: CallbackQuery) -> None:
    account_id = int((callback.data or "").split(":")[-1])
    async with async_session_factory() as session:
        account = await TelegramAccountRepository(session).toggle_mailing(account_id)
        await session.commit()

    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    if account.status != "active":
        await callback.answer("Можно менять только авторизованный аккаунт", show_alert=True)
        return
    if account.telegram_user_id in settings.admin_ids and account.is_mailing_enabled:
        async with async_session_factory() as session:
            await TelegramAccountRepository(session).toggle_mailing(account_id)
            await session.commit()
        await callback.answer("Админский аккаунт нельзя добавить в рассылку", show_alert=True)
        return

    await callback.message.answer("Настройки рассылки аккаунта обновлены.", reply_markup=main_menu())
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


class IsraelDiscoveryReporter:
    def __init__(self, message: Message) -> None:
        self.message = message

    async def send(self) -> None:
        await self.message.answer(
            "Добираю базу израильских каналов от seed-каналов."
        )

        try:
            result = await IsraelChannelDiscoverer().discover()
        except Exception as error:
            logger.exception("Israel channel discovery failed")
            async with async_session_factory() as session:
                await LogRepository(session).error(
                    "israel_channel_discovery_failed",
                    str(error),
                    payload={"exception_type": type(error).__name__},
                )
                await session.commit()
            await self.message.answer(
                "Импорт израильских каналов не выполнен. "
                "Подробности записаны в logs/app.log.",
                reply_markup=main_menu(),
            )
            return

        summary = (
            "Импорт израильских каналов завершён.\n"
            f"Цель в базе: {result.target_total}\n"
            f"Было каналов: {result.channels_total_before}\n"
            f"Стало каналов: {result.channels_total_after}\n"
            f"Seed-каналов: {result.seed_channels}\n"
            f"Search-запросов: {result.search_queries}\n"
            f"Search-кандидатов: {result.search_candidates}\n"
            f"Проверено: {result.scanned_channels}\n"
            f"Подошло: {result.matched_channels}\n"
            f"Найдено упоминаний: {result.discovered_mentions}\n"
            f"Найдено пересылок: {result.forwarded_mentions}\n"
            f"Глубина: {result.max_depth}\n"
            f"Добавлено: {result.channels_added}\n"
            f"Уже было: {result.channels_existing}\n"
            f"Пропущено: {result.channels_skipped}\n"
            f"Остановка: {result.stopped_reason or '-'}"
        )
        await self.message.answer(summary, reply_markup=main_menu())

        if result.errors:
            await self.message.answer("Ошибки: " + "; ".join(result.errors[:5]))

        if not result.added_usernames:
            return

        lines = result.added_usernames[:TGSTAT_PREVIEW_LIMIT]
        if len(result.added_usernames) > TGSTAT_PREVIEW_LIMIT:
            lines.append(f"...ещё {len(result.added_usernames) - TGSTAT_PREVIEW_LIMIT}")

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
