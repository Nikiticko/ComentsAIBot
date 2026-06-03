from dataclasses import dataclass, field
import logging
import random
from pathlib import Path

from telethon.errors import RPCError

from comments_ai_bot.ai.service import AiService
from comments_ai_bot.core.config import settings
from comments_ai_bot.core.types import LogLevel, PostStatus
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    LogRepository,
    PostRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.monitoring.manual_scan import POST_SCAN_HOURS, POST_SCAN_LIMIT
from comments_ai_bot.telegram_client.client import TelegramAccountClient

MAX_CHANNEL_ATTEMPTS = 20
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiTopicTestPost:
    channel_username: str
    telegram_post_id: int
    text: str
    views_count: int | None
    topic_analysis: dict

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel_username.removeprefix('@')}/{self.telegram_post_id}"


@dataclass
class AiTopicTestResult:
    post: AiTopicTestPost | None = None
    account: str | None = None
    channels_total: int = 0
    channels_attempted: int = 0
    posts_checked: int = 0
    errors: list[str] = field(default_factory=list)


class AiTopicTester:
    def __init__(self, ai_service: AiService | None = None) -> None:
        self.ai_service = ai_service or AiService()

    async def analyze_random_commentable_post(self) -> AiTopicTestResult:
        result = AiTopicTestResult()

        async with async_session_factory() as session:
            channels = await ChannelRepository(session).list_active()
            accounts = await TelegramAccountRepository(session).list_active()
            result.channels_total = len(channels)

        if not channels:
            result.errors.append("Нет активных каналов.")
            return result

        legacy_session_exists = Path("data", f"{settings.telegram_session_name}.session").exists()
        if not accounts and not legacy_session_exists:
            result.errors.append("Нет активного Telegram-аккаунта.")
            return result

        random.shuffle(channels)
        account_sessions = [account.session_name for account in accounts] or [None]
        account_ids = {account.session_name: account.id for account in accounts}
        result.account = ", ".join(
            session_name or settings.telegram_session_name for session_name in account_sessions
        )

        for index, channel in enumerate(channels[:MAX_CHANNEL_ATTEMPTS]):
            session_name = account_sessions[index % len(account_sessions)]
            result.channels_attempted += 1

            try:
                async with TelegramAccountClient(session_name) as telegram:
                    found_post = await self._find_commentable_post(channel, telegram, result)
            except (RPCError, ValueError, RuntimeError) as error:
                logger.warning("AI topic test skipped channel %s: %s", channel.username, error)
                result.errors.append(f"{channel.username}: {error}")
                continue

            if session_name is not None:
                async with async_session_factory() as session:
                    await TelegramAccountRepository(session).mark_used(account_ids[session_name])
                    await session.commit()

            if found_post is not None:
                result.post = found_post
                return result

        if result.post is None:
            result.errors.append("Не найден пост с текстом и открытыми комментариями.")
        return result

    async def _find_commentable_post(
        self,
        channel,
        telegram: TelegramAccountClient,
        result: AiTopicTestResult,
    ) -> AiTopicTestPost | None:
        posts = await telegram.fetch_recent_posts(
            channel.username,
            limit=POST_SCAN_LIMIT,
            hours=POST_SCAN_HOURS,
        )
        random.shuffle(posts)

        for post in posts:
            text = (post.text or "").strip()
            if not text:
                continue

            result.posts_checked += 1
            availability = await telegram.can_comment(channel.username, post.id, post.message_ids)
            if not availability.available:
                continue

            topic_analysis = await self.ai_service.analyze_topic(text)
            await self._save_post(channel, post, topic_analysis)
            await self._write_log(
                LogLevel.INFO,
                "ai_topic_test_completed",
                f"ИИ определил тему поста {self._post_url(channel.username, post.id)}",
                "channel",
                channel.id,
                payload={"topic_analysis": topic_analysis},
            )
            return AiTopicTestPost(
                channel_username=channel.username,
                telegram_post_id=post.id,
                text=text,
                views_count=post.views,
                topic_analysis=topic_analysis,
            )

        return None

    async def _save_post(self, channel, post, topic_analysis: dict) -> None:
        async with async_session_factory() as session:
            db_post = await PostRepository(session).upsert(
                channel_id=channel.id,
                telegram_post_id=post.id,
                text=post.text,
                views_count=post.views or 0,
                status=PostStatus.READY_TO_COMMENT.value,
            )
            db_post.topic_analysis = topic_analysis
            await session.commit()

    async def _write_log(
        self,
        level: LogLevel,
        event: str,
        message: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        payload: dict | None = None,
    ) -> None:
        async with async_session_factory() as session:
            await LogRepository(session).create(
                level,
                event,
                message,
                entity_type,
                entity_id,
                payload,
            )
            await session.commit()

    def _post_url(self, channel_username: str, post_id: int) -> str:
        return f"https://t.me/{channel_username.removeprefix('@')}/{post_id}"
