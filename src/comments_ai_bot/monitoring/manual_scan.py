from dataclasses import dataclass, field
import logging
from pathlib import Path

from telethon.errors import RPCError

from comments_ai_bot.core.config import settings
from comments_ai_bot.core.types import PostStatus
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    LogRepository,
    PostRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.filtering.rules import MIN_POST_VIEWS
from comments_ai_bot.telegram_client.client import TelegramAccountClient

POST_SCAN_LIMIT = 100
POST_SCAN_HOURS = 24
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HighViewPost:
    channel_username: str
    telegram_post_id: int
    views_count: int
    text: str | None
    date: str

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel_username.removeprefix('@')}/{self.telegram_post_id}"


@dataclass
class ScanResult:
    channels_total: int = 0
    channels_failed: int = 0
    posts_checked: int = 0
    posts_saved: int = 0
    scan_hours: int = POST_SCAN_HOURS
    errors: list[str] = field(default_factory=list)
    channel_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    high_view_posts: list[HighViewPost] = field(default_factory=list)


class ManualPostScanner:
    async def scan_high_view_posts(self) -> ScanResult:
        result = ScanResult()
        logger.info("Manual high-view post scan started")

        async with async_session_factory() as session:
            channels = await ChannelRepository(session).list_active()
            account = await TelegramAccountRepository(session).get_active()
            result.channels_total = len(channels)

        if not channels:
            logger.info("Manual scan skipped: no active channels")
            return result
        session_name = account.session_name if account is not None else None
        legacy_session_exists = Path("data", f"{settings.telegram_session_name}.session").exists()
        if account is None and not legacy_session_exists:
            result.errors.append("Нет активного Telegram-аккаунта. Открой 'Аккаунты TG' и добавь аккаунт.")
            logger.warning("Manual scan skipped: no active telegram account")
            return result
        if account is None:
            logger.warning("Using legacy Telegram session; add account via 'Аккаунты TG' to manage it")

        try:
            async with TelegramAccountClient(session_name) as telegram:
                for channel in channels:
                    logger.info("Scanning channel %s", channel.username)
                    try:
                        posts = await telegram.fetch_recent_posts(
                            channel.username,
                            limit=POST_SCAN_LIMIT,
                            hours=POST_SCAN_HOURS,
                        )
                    except (RPCError, ValueError, RuntimeError) as error:
                        result.channels_failed += 1
                        result.errors.append(f"{channel.username}: {error}")
                        logger.exception("Failed to scan channel %s", channel.username)
                        async with async_session_factory() as session:
                            await LogRepository(session).error(
                                "channel_scan_failed",
                                f"Не удалось спарсить {channel.username}: {error}",
                                "channel",
                                channel.id,
                                payload={"exception_type": type(error).__name__},
                            )
                            await session.commit()
                        continue

                    result.posts_checked += len(posts)
                    result.channel_stats[channel.username] = {
                        "checked": len(posts),
                        "high_view": 0,
                    }

                    async with async_session_factory() as session:
                        post_repo = PostRepository(session)
                        log_repo = LogRepository(session)

                        for post in posts:
                            views_count = post.views or 0
                            if views_count >= MIN_POST_VIEWS:
                                status = PostStatus.READY_TO_COMMENT.value
                                skip_reason = None
                                result.channel_stats[channel.username]["high_view"] += 1
                                result.high_view_posts.append(
                                    HighViewPost(
                                        channel_username=channel.username,
                                        telegram_post_id=post.id,
                                        views_count=views_count,
                                        text=post.text,
                                        date=post.date.strftime("%Y-%m-%d %H:%M"),
                                    )
                                )
                            else:
                                status = PostStatus.VIEWS_TOO_LOW.value
                                skip_reason = "Недостаточно просмотров"

                            await post_repo.upsert(
                                channel_id=channel.id,
                                telegram_post_id=post.id,
                                text=post.text,
                                views_count=views_count,
                                status=status,
                                skip_reason=skip_reason,
                            )
                            result.posts_saved += 1

                        await log_repo.info(
                            "channel_scanned",
                            f"Канал {channel.username}: проверено постов за 24 часа {len(posts)}",
                            "channel",
                            channel.id,
                        )
                        await session.commit()
        except RuntimeError as error:
            result.errors.append(str(error))
            logger.exception("Manual high-view post scan failed")
            async with async_session_factory() as session:
                await LogRepository(session).error(
                    "manual_scan_failed",
                    str(error),
                    payload={"exception_type": type(error).__name__},
                )
                await session.commit()

        logger.info(
            "Manual high-view post scan finished: channels=%s failed=%s checked=%s saved=%s high_view=%s",
            result.channels_total,
            result.channels_failed,
            result.posts_checked,
            result.posts_saved,
            len(result.high_view_posts),
        )
        return result
