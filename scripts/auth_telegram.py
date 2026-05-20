import asyncio
import getpass
import logging
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from comments_ai_bot.core.config import settings
from comments_ai_bot.core.logging import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    Path("data").mkdir(exist_ok=True)
    session_path = Path("data") / settings.telegram_session_name

    client = TelegramClient(
        str(session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )

    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            if me.bot:
                logger.warning("Telethon session is authorized as bot")
                print("Текущая Telethon-сессия авторизована как бот.")
                print(f"Удали файл data/{settings.telegram_session_name}.session")
                print("Потом запусти этот скрипт снова и введи номер обычного Telegram-аккаунта.")
                return

            print(f"Telegram-аккаунт уже авторизован: {me.username or me.id}")
            return

        phone = input("Введите номер обычного Telegram-аккаунта, например +380XXXXXXXXX: ").strip()
        if ":" in phone:
            logger.warning("Bot token entered instead of phone number")
            print("Похоже, введён bot token. Для парсинга нужен обычный Telegram-аккаунт.")
            return

        await client.send_code_request(phone)
        code = input("Введите код из Telegram: ").strip()

        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = getpass.getpass("Введите пароль 2FA Telegram: ")
            await client.sign_in(password=password)

        me = await client.get_me()
        if me.bot:
            logger.warning("Authorized identity is bot")
            print("Авторизован бот, но для парсинга нужен обычный Telegram-аккаунт.")
            return

        logger.info("Telegram user session authorized")
        print(f"Telegram-аккаунт авторизован: {me.username or me.id}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
