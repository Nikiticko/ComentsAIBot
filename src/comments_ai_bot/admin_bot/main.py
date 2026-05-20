import asyncio
import logging

from aiogram import Bot, Dispatcher

from comments_ai_bot.admin_bot.errors import log_unhandled_error
from comments_ai_bot.admin_bot.handlers import router
from comments_ai_bot.admin_bot.middleware import AdminOnlyMiddleware
from comments_ai_bot.core.config import settings
from comments_ai_bot.core.logging import setup_logging


async def run() -> None:
    setup_logging()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(handle_asyncio_exception)

    bot = Bot(token=settings.admin_bot_token)
    dispatcher = Dispatcher()
    dispatcher.update.middleware(AdminOnlyMiddleware())
    dispatcher.errors.register(log_unhandled_error)
    dispatcher.include_router(router)

    await dispatcher.start_polling(bot)


def handle_asyncio_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exception = context.get("exception")
    message = context.get("message", "Unhandled asyncio exception")
    if exception is None:
        logging.getLogger(__name__).error(message)
        return

    logging.getLogger(__name__).exception(message, exc_info=exception)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
