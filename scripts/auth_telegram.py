import asyncio
import sys
from pathlib import Path

from telethon import TelegramClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from comments_ai_bot.core.config import settings


async def main() -> None:
    Path("data").mkdir(exist_ok=True)
    session_path = Path("data") / settings.telegram_session_name

    client = TelegramClient(
        str(session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )

    async with client:
        await client.start()
        me = await client.get_me()
        print(f"Telegram-аккаунт авторизован: {me.username or me.id}")


if __name__ == "__main__":
    asyncio.run(main())
