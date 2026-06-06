import asyncio
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import shutil

import qrcode
from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError

from comments_ai_bot.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramAuthResult:
    ok: bool
    message: str
    needs_password: bool = False


def session_path(session_name: str | None = None) -> Path:
    if session_name is None:
        session_name = settings.telegram_session_name
    return Path("data") / "accounts" / session_name


def legacy_session_path(session_name: str | None = None) -> Path:
    if session_name is None:
        session_name = settings.telegram_session_name
    return Path("data") / session_name


def remove_session_files(session_name: str | None = None) -> None:
    base_path = session_path(session_name)
    for path in (base_path.with_suffix(".session"), base_path.with_suffix(".session-journal")):
        if path.exists():
            path.unlink()


def copy_legacy_session_files(session_name: str | None = None) -> None:
    legacy_base_path = legacy_session_path(session_name)
    account_base_path = session_path(session_name)
    account_base_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".session", ".session-journal"):
        source = legacy_base_path.with_suffix(suffix)
        if source.exists():
            shutil.copy2(source, account_base_path.with_suffix(suffix))


def build_qr_png(url: str) -> bytes:
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(url)
    image = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def create_client(session_name: str | None = None) -> TelegramClient:
    (Path("data") / "accounts").mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(session_path(session_name)),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        proxy=settings.telegram_proxy,
    )
    await client.connect()
    return client


async def create_legacy_client(session_name: str | None = None) -> TelegramClient:
    client = TelegramClient(
        str(legacy_session_path(session_name)),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        proxy=settings.telegram_proxy,
    )
    await client.connect()
    return client


async def get_auth_status(session_name: str | None = None) -> TelegramAuthResult:
    client = await create_client(session_name)
    try:
        if not await client.is_user_authorized():
            return TelegramAuthResult(False, "Telegram-аккаунт не авторизован.")

        me = await client.get_me()
        if me.bot:
            return TelegramAuthResult(False, "Сейчас сохранена bot-сессия, нужна user-сессия.")

        return TelegramAuthResult(True, f"Telegram-аккаунт авторизован: {me.username or me.id}")
    finally:
        await client.disconnect()


async def start_qr_login(session_name: str | None = None) -> tuple[TelegramClient, object, bytes]:
    client = await create_client(session_name)

    if await client.is_user_authorized():
        me = await client.get_me()
        if me.bot:
            await client.disconnect()
            remove_session_files(session_name)
            client = await create_client(session_name)
        else:
            await client.disconnect()
            raise RuntimeError(f"Telegram-аккаунт уже авторизован: {me.username or me.id}")

    qr_login = await client.qr_login()
    return client, qr_login, build_qr_png(qr_login.url)


async def wait_qr_login(
    client: TelegramClient,
    qr_login: object,
    *,
    session_name: str | None = None,
    timeout: int = 120,
) -> TelegramAuthResult:
    should_disconnect = True
    try:
        try:
            await qr_login.wait(timeout=timeout)
        except SessionPasswordNeededError:
            should_disconnect = False
            return TelegramAuthResult(
                False,
                "На аккаунте включён 2FA-пароль. Отправь пароль Telegram одним сообщением.",
                needs_password=True,
            )
        except asyncio.TimeoutError:
            return TelegramAuthResult(False, "Время ожидания QR-кода истекло. Нажми кнопку авторизации ещё раз.")

        me = await client.get_me()
        if me.bot:
            remove_session_files(session_name)
            return TelegramAuthResult(False, "Авторизован бот, а нужен обычный Telegram-аккаунт.")

        logger.info("Telegram user authorized by QR")
        return TelegramAuthResult(True, f"Telegram-аккаунт авторизован: {me.username or me.id}")
    finally:
        if should_disconnect:
            await client.disconnect()


async def finish_password_login(
    client: TelegramClient,
    password: str,
    *,
    session_name: str | None = None,
) -> TelegramAuthResult:
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        return TelegramAuthResult(
            False,
            "Неверный 2FA-пароль. Отправь правильный пароль Telegram или нажми Отмена.",
            needs_password=True,
        )

    me = await client.get_me()
    if me.bot:
        remove_session_files(session_name)
        return TelegramAuthResult(False, "Авторизован бот, а нужен обычный Telegram-аккаунт.")

    logger.info("Telegram user authorized by QR and 2FA password")
    return TelegramAuthResult(True, f"Telegram-аккаунт авторизован: {me.username or me.id}")
