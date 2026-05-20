from dataclasses import dataclass, field

from telethon.errors import RPCError

from comments_ai_bot.core.types import PostStatus
from comments_ai_bot.db.repositories import ChannelRepository, LogRepository, PostRepository
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.filtering.rules import MIN_POST_VIEWS
from comments_ai_bot.telegram_client.client import TelegramAccountClient

POST_SCAN_LIMIT = 100
POST_SCAN_HOURS = 24


@dataclass(frozen=True)
class HighViewPost:
    channel_username: str
    telegram_post_id: int
    views_count: int
    text: str | None

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
    high_view_posts: list[HighViewPost] = field(default_factory=list)


class ManualPostScanner:
    async def scan_high_view_posts(self) -> ScanResult:
        result = ScanResult()

        async with async_session_factory() as session:
            channels = await ChannelRepository(session).list_active()
            result.channels_total = len(channels)

        async with TelegramAccountClient() as telegram:
            for channel in channels:
                try:
                    posts = await telegram.fetch_recent_posts(
                        channel.username,
                        limit=POST_SCAN_LIMIT,
                        hours=POST_SCAN_HOURS,
                    )
                except (RPCError, ValueError, RuntimeError) as error:
                    result.channels_failed += 1
                    async with async_session_factory() as session:
                        await LogRepository(session).info(
                            "channel_scan_failed",
                            f"Не удалось спарсить {channel.username}: {error}",
                            "channel",
                            channel.id,
                        )
                        await session.commit()
                    continue

                result.posts_checked += len(posts)

                async with async_session_factory() as session:
                    post_repo = PostRepository(session)
                    log_repo = LogRepository(session)

                    for post in posts:
                        views_count = post.views or 0
                        if views_count >= MIN_POST_VIEWS:
                            status = PostStatus.READY_TO_COMMENT.value
                            skip_reason = None
                            result.high_view_posts.append(
                                HighViewPost(
                                    channel_username=channel.username,
                                    telegram_post_id=post.id,
                                    views_count=views_count,
                                    text=post.text,
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

        return result
