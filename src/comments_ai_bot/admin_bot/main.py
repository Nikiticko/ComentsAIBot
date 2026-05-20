import asyncio
import logging

from aiogram import Bot, Dispatcher

from comments_ai_bot.admin_bot.handlers import router
from comments_ai_bot.admin_bot.middleware import AdminOnlyMiddleware
from comments_ai_bot.core.config import settings


async def run() -> None:
    logging.basicConfig(level=settings.log_level)

    bot = Bot(token=settings.admin_bot_token)
    dispatcher = Dispatcher()
    dispatcher.update.middleware(AdminOnlyMiddleware())
    dispatcher.include_router(router)

    await dispatcher.start_polling(bot)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
