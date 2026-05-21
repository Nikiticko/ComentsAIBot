import asyncio
from dataclasses import dataclass, field
import logging
import random
from pathlib import Path

from telethon.errors import ChatWriteForbiddenError, RPCError, UserBannedInChannelError

from comments_ai_bot.core.config import settings
from comments_ai_bot.core.types import CommentStatus, LogLevel, PostStatus
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    CommentRepository,
    LogRepository,
    PostRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.monitoring.manual_scan import POST_SCAN_HOURS, POST_SCAN_LIMIT
from comments_ai_bot.telegram_client.client import CommentAvailability, TelegramAccountClient

TEST_COMMENT_LIMIT = 3
TEST_COMMENT_DELAY_SECONDS = 8
TEST_COMMENT_TEXTS = (
    "четко",
    "согласен",
    "хорошо сказано",
    "в точку",
    "интересно",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TestCommentItem:
    post_url: str
    status: str
    reason: str | None = None


@dataclass
class TestCommentResult:
    channel_username: str | None = None
    account: str | None = None
    posts_found: int = 0
    comments_sent: int = 0
    comments_failed: int = 0
    comments_skipped: int = 0
    stopped_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    items: list[TestCommentItem] = field(default_factory=list)


class TestCommentSender:
    async def send_to_random_channel(self) -> TestCommentResult:
        result = TestCommentResult()

        async with async_session_factory() as session:
            channels = await ChannelRepository(session).list_active()
            accounts = await TelegramAccountRepository(session).list_active()

        if not channels:
            result.errors.append("Нет активных каналов.")
            return result

        legacy_session_exists = Path("data", f"{settings.telegram_session_name}.session").exists()
        if not accounts and not legacy_session_exists:
            result.errors.append("Нет активного Telegram-аккаунта.")
            return result

        channel = random.choice(channels)
        account = random.choice(accounts) if accounts else None
        session_name = account.session_name if account else None
        result.channel_username = channel.username
        result.account = session_name or settings.telegram_session_name

        try:
            async with TelegramAccountClient(session_name) as telegram:
                posts = await telegram.fetch_recent_posts(
                    channel.username,
                    limit=POST_SCAN_LIMIT,
                    hours=POST_SCAN_HOURS,
                )
                random.shuffle(posts)
                result.posts_found = len(posts)

                for post in posts:
                    if result.comments_sent >= TEST_COMMENT_LIMIT:
                        result.stopped_reason = f"Достигнут лимит {TEST_COMMENT_LIMIT} комментария."
                        break

                    should_continue = await self._send_to_post(channel, post, telegram, result)
                    if not should_continue:
                        break

                    if result.comments_sent:
                        await asyncio.sleep(TEST_COMMENT_DELAY_SECONDS)

            if account is not None:
                async with async_session_factory() as session:
                    await TelegramAccountRepository(session).mark_used(account.id)
                    await session.commit()
        except (RPCError, ValueError, RuntimeError) as error:
            logger.exception("Test comment sending failed")
            result.errors.append(str(error))
            message = f"Тестовая отправка комментариев не выполнена: {error}"
            await self._write_log(
                LogLevel.ERROR,
                "test_comments_failed",
                message,
            )

        return result

    async def _send_to_post(
        self,
        channel,
        post,
        telegram: TelegramAccountClient,
        result: TestCommentResult,
    ) -> bool:
        post_url = f"https://t.me/{channel.username.removeprefix('@')}/{post.id}"
        availability = await self._check_comments(channel.username, post, telegram)

        if not availability.available:
            result.comments_skipped += 1
            result.items.append(TestCommentItem(post_url, "skipped", availability.reason))
            await self._save_post(
                channel,
                post,
                PostStatus.COMMENTS_CLOSED.value,
                availability.reason,
            )
            return True

        db_post_id = await self._save_post(channel, post, PostStatus.READY_TO_COMMENT.value)
        comment_text = random.choice(TEST_COMMENT_TEXTS)
        comment_post_ids = (availability.post_id or post.id,)

        try:
            telegram_comment_id = await telegram.send_comment(
                channel.username,
                post.id,
                comment_text,
                comment_post_ids,
            )
        except (ChatWriteForbiddenError, UserBannedInChannelError) as error:
            logger.warning("Test comment stopped for %s/%s: %s", channel.username, post.id, error)
            result.stopped_reason = f"Аккаунт не может писать в обсуждение: {error}"
            result.items.append(TestCommentItem(post_url, "stopped", result.stopped_reason))
            await self._write_log(
                LogLevel.WARNING,
                "test_comments_stopped",
                f"Тест остановлен: {post_url} {error}",
                "post",
                db_post_id,
            )
            return False
        except (RPCError, ValueError, RuntimeError) as error:
            logger.warning("Failed to send test comment to %s/%s: %s", channel.username, post.id, error)
            result.comments_failed += 1
            result.items.append(TestCommentItem(post_url, "failed", str(error)))
            await self._save_comment(
                db_post_id,
                CommentStatus.FAILED.value,
                comment_text,
                error_message=str(error),
            )
            await self._write_log(
                LogLevel.ERROR,
                "test_comment_failed",
                f"Не удалось отправить тестовый комментарий: {post_url} {error}",
                "post",
                db_post_id,
            )
            return True

        result.comments_sent += 1
        result.items.append(TestCommentItem(post_url, "sent"))
        await self._save_comment(
            db_post_id,
            CommentStatus.PUBLISHED.value,
            comment_text,
            telegram_comment_id=telegram_comment_id,
        )
        await self._write_log(
            LogLevel.INFO,
            "test_comment_sent",
            f"Тестовый комментарий отправлен в {post_url}",
            "post",
            db_post_id,
        )
        return True

    async def _check_comments(
        self,
        channel_username: str,
        post,
        telegram: TelegramAccountClient,
    ) -> CommentAvailability:
        try:
            return await telegram.can_comment(channel_username, post.id, post.message_ids)
        except (RPCError, ValueError, RuntimeError) as error:
            return CommentAvailability(
                False,
                f"Ошибка проверки комментариев: {error}",
            )

    async def _save_post(
        self,
        channel,
        post,
        status: str,
        skip_reason: str | None = None,
    ) -> int:
        async with async_session_factory() as session:
            db_post = await PostRepository(session).upsert(
                channel_id=channel.id,
                telegram_post_id=post.id,
                text=post.text,
                views_count=post.views or 0,
                status=status,
                skip_reason=skip_reason,
            )
            await session.commit()
            return db_post.id

    async def _save_comment(
        self,
        post_id: int,
        status: str,
        text: str,
        *,
        telegram_comment_id: int | None = None,
        error_message: str | None = None,
    ) -> None:
        async with async_session_factory() as session:
            await CommentRepository(session).create(
                post_id=post_id,
                text=text,
                status=status,
                telegram_comment_id=telegram_comment_id,
                error_message=error_message,
            )
            await session.commit()

    async def _write_log(
        self,
        level: LogLevel,
        event: str,
        message: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
    ) -> None:
        async with async_session_factory() as session:
            await LogRepository(session).create(level, event, message, entity_type, entity_id)
            await session.commit()
