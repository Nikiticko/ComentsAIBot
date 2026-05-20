import asyncio
import getpass
import logging
import sys
from pathlib import Path
from typing import Literal

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from comments_ai_bot.core.config import settings
from comments_ai_bot.core.logging import setup_logging

logger = logging.getLogger(__name__)
AuthMode = Literal["qr", "phone"]


def print_code_hint() -> None:
    print("Код запрошен.")
    print("Ищи его в приложении Telegram на этом аккаунте, не в админ-боте.")
    print("Если Telegram открыт на нескольких устройствах, проверь каждое.")


async def request_login_code(client: TelegramClient, phone: str) -> str:
    print("Запрашиваю код входа...")
    await client.send_code_request(phone)
    print_code_hint()

    return input("Введите код из Telegram. Если кода нет, нажми Enter для остановки: ").strip()


def choose_auth_mode() -> AuthMode:
    print("Выбери способ авторизации:")
    print("1 - QR-код через Telegram на телефоне")
    print("2 - номер телефона и код")

    value = input("Способ [1]: ").strip()
    if value == "2":
        return "phone"
    return "qr"


def print_qr(url: str) -> bool:
    try:
        import qrcode
    except ImportError:
        print("Не установлен пакет qrcode.")
        print("Выполни: python -m pip install -e .")
        print(f"QR URL: {url}")
        return False

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)
    return True


async def authorize_by_qr(client: TelegramClient) -> bool:
    print("Генерирую QR-код для входа...")
    qr_login = await client.qr_login()
    print_qr(qr_login.url)
    print("Открой Telegram на телефоне: Настройки -> Устройства -> Подключить устройство.")
    print("Сканируй QR-код из терминала. Ожидаю до 2 минут.")

    try:
        await qr_login.wait(timeout=120)
    except SessionPasswordNeededError:
        password = getpass.getpass("Введите пароль 2FA Telegram: ")
        await client.sign_in(password=password)
    except asyncio.TimeoutError:
        print("Время ожидания QR-кода истекло. Запусти скрипт снова.")
        return False

    return True


async def authorize_by_phone(client: TelegramClient) -> bool:
    phone = input("Введите номер обычного Telegram-аккаунта, например +380XXXXXXXXX: ").strip()
    if ":" in phone:
        logger.warning("Bot token entered instead of phone number")
        print("Похоже, введён bot token. Для парсинга нужен обычный Telegram-аккаунт.")
        return False

    code = await request_login_code(client, phone)
    if not code:
        print("Код не введён. Авторизация остановлена.")
        return False

    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        password = getpass.getpass("Введите пароль 2FA Telegram: ")
        await client.sign_in(password=password)

    return True


async def main() -> None:
    setup_logging()
    Path("data").mkdir(exist_ok=True)
    session_path = Path("data") / settings.telegram_session_name

    client = TelegramClient(
        str(session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )

    try:
        print("Подключаюсь к Telegram...")
        await client.connect()

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

        auth_mode = choose_auth_mode()
        authorized = (
            await authorize_by_qr(client)
            if auth_mode == "qr"
            else await authorize_by_phone(client)
        )
        if not authorized:
            return

        me = await client.get_me()
        if me.bot:
            logger.warning("Authorized identity is bot")
            print("Авторизован бот, но для парсинга нужен обычный Telegram-аккаунт.")
            return

        logger.info("Telegram user session authorized")
        print(f"Telegram-аккаунт авторизован: {me.username or me.id}")
    except PhoneNumberInvalidError:
        logger.exception("Invalid Telegram phone number")
        print("Telegram не принял номер телефона. Проверь формат, например +380XXXXXXXXX.")
    except PhoneCodeInvalidError:
        logger.exception("Invalid Telegram login code")
        print("Неверный код. Запусти скрипт снова и введи новый код из Telegram.")
    except PhoneCodeExpiredError:
        logger.exception("Telegram login code expired")
        print("Код истёк. Запусти скрипт снова и запроси новый код.")
    except FloodWaitError as error:
        logger.exception("Telegram flood wait while authorizing")
        print(f"Telegram временно ограничил попытки. Подожди {error.seconds} секунд.")
    except SendCodeUnavailableError:
        logger.exception("Telegram code delivery unavailable")
        print("Telegram больше не даёт запросить код для этого номера прямо сейчас.")
        print("Обычно это значит, что варианты доставки уже использованы или Telegram поставил паузу.")
        print("Подожди 30-60 минут и попробуй снова. SMS-принудительно запросить уже нельзя.")
    except ApiIdInvalidError:
        logger.exception("Invalid Telegram API ID/API HASH")
        print("Telegram не принял TELEGRAM_API_ID или TELEGRAM_API_HASH. Проверь данные с my.telegram.org.")
    except Exception:
        logger.exception("Unexpected Telegram authorization error")
        print("Неожиданная ошибка авторизации. Подробности записаны в logs/app.log.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
