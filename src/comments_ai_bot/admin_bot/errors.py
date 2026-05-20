import logging

from aiogram.types import ErrorEvent

from comments_ai_bot.db.repositories import LogRepository
from comments_ai_bot.db.session import async_session_factory

logger = logging.getLogger(__name__)


async def log_unhandled_error(event: ErrorEvent) -> bool:
    logger.exception("Unhandled aiogram update error", exc_info=event.exception)

    try:
        async with async_session_factory() as session:
            await LogRepository(session).error(
                "unhandled_aiogram_error",
                str(event.exception),
                payload={"exception_type": type(event.exception).__name__},
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to save unhandled aiogram error to database")

    return True
