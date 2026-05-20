import asyncio
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from comments_ai_bot.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramAuthResult:
    ok: bool
    message: str


def session_path() -> Path:
    return Path("data") / settings.telegram_session_name


def remove_session_files() -> None:
    base_path = session_path()
    for path in (base_path.with_suffix(".session"), base_path.with_suffix(".session-journal")):
        if path.exists():
            path.unlink()


def build_qr_png(url: str) -> bytes:
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(url)
    image = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def create_client() -> TelegramClient:
    Path("data").mkdir(exist_ok=True)
    client = TelegramClient(
        str(session_path()),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.connect()
    return client


async def get_auth_status() -> TelegramAuthResult:
    client = await create_client()
    try:
        if not await client.is_user_authorized():
            return TelegramAuthResult(False, "Telegram-аккаунт не авторизован.")

        me = await client.get_me()
        if me.bot:
            return TelegramAuthResult(False, "Сейчас сохранена bot-сессия, нужна user-сессия.")

        return TelegramAuthResult(True, f"Telegram-аккаунт авторизован: {me.username or me.id}")
    finally:
        await client.disconnect()


async def start_qr_login() -> tuple[TelegramClient, object, bytes]:
    client = await create_client()

    if await client.is_user_authorized():
        me = await client.get_me()
        if me.bot:
            await client.disconnect()
            remove_session_files()
            client = await create_client()
        else:
            await client.disconnect()
            raise RuntimeError(f"Telegram-аккаунт уже авторизован: {me.username or me.id}")

    qr_login = await client.qr_login()
    return client, qr_login, build_qr_png(qr_login.url)


async def wait_qr_login(client: TelegramClient, qr_login: object, timeout: int = 120) -> TelegramAuthResult:
    try:
        try:
            await qr_login.wait(timeout=timeout)
        except SessionPasswordNeededError:
            return TelegramAuthResult(
                False,
                "На аккаунте включён 2FA-пароль. Для такой авторизации пока используй scripts/auth_telegram.py.",
            )
        except asyncio.TimeoutError:
            return TelegramAuthResult(False, "Время ожидания QR-кода истекло. Нажми кнопку авторизации ещё раз.")

        me = await client.get_me()
        if me.bot:
            remove_session_files()
            return TelegramAuthResult(False, "Авторизован бот, а нужен обычный Telegram-аккаунт.")

        logger.info("Telegram user authorized by QR")
        return TelegramAuthResult(True, f"Telegram-аккаунт авторизован: {me.username or me.id}")
    finally:
        await client.disconnect()
