from dataclasses import dataclass, field
import logging
import random
from pathlib import Path

from openai import OpenAIError
from telethon.errors import RPCError

from comments_ai_bot.ai.service import MIN_AI_CONTEXT_TEXT_CHARS, AiService
from comments_ai_bot.core.config import settings
from comments_ai_bot.core.types import LogLevel, PostStatus
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    LogRepository,
    PostRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.filtering.validation import PostValidationResult, PostValidator
from comments_ai_bot.monitoring.manual_scan import POST_SCAN_HOURS, POST_SCAN_LIMIT
from comments_ai_bot.telegram_client.client import (
    TelegramAccountClient,
    is_missing_username_error,
)

MAX_CHANNEL_ATTEMPTS = 20
MIN_AI_TEST_TEXT_CHARS = MIN_AI_CONTEXT_TEXT_CHARS
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiTopicTestPost:
    channel_username: str
    telegram_post_id: int
    text: str
    views_count: int | None
    validation: PostValidationResult
    generated_comment: str | None = None
    comment_validation: dict | None = None

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
    posts_reached_ai: int = 0
    posts_without_text: int = 0
    posts_too_short: int = 0
    posts_comments_closed: int = 0
    broken_channels: int = 0
    errors: list[str] = field(default_factory=list)


class AiTopicTester:
    def __init__(self, ai_service: AiService | None = None) -> None:
        self.ai_service = ai_service or AiService()
        self.validator = PostValidator(self.ai_service)

    async def analyze_random_commentable_post(self) -> AiTopicTestResult:
        result = AiTopicTestResult()

        async with async_session_factory() as session:
            channels = await ChannelRepository(session).list_active()
            accounts = await TelegramAccountRepository(session).list_active()
            result.channels_total = len(channels)

        if not channels:
            result.errors.append("Нет активных каналов.")
            await self._write_log(
                LogLevel.WARNING,
                "ai_topic_test_no_channels",
                "Тест ИИ остановлен: нет активных каналов.",
            )
            return result

        legacy_session_exists = Path("data", f"{settings.telegram_session_name}.session").exists()
        if not accounts and not legacy_session_exists:
            result.errors.append("Нет активного Telegram-аккаунта.")
            await self._write_log(
                LogLevel.ERROR,
                "ai_topic_test_no_account",
                "Тест ИИ остановлен: нет активного Telegram-аккаунта.",
            )
            return result

        random.shuffle(channels)
        account_sessions = [account.session_name for account in accounts] or [None]
        account_ids = {account.session_name: account.id for account in accounts}
        result.account = ", ".join(
            session_name or settings.telegram_session_name for session_name in account_sessions
        )
        await self._write_log(
            LogLevel.INFO,
            "ai_topic_test_started",
            "Запущен тест валидации поста через триггеры и ИИ.",
            payload={
                "channels_total": result.channels_total,
                "max_channel_attempts": MAX_CHANNEL_ATTEMPTS,
                "accounts_count": len(account_sessions),
                "account": result.account,
                "model": settings.openai_model,
                "post_scan_limit": POST_SCAN_LIMIT,
                "post_scan_hours": POST_SCAN_HOURS,
                "min_text_chars": MIN_AI_TEST_TEXT_CHARS,
                "trigger_words_count": len(settings.post_trigger_words),
                "forbidden_topics_count": len(settings.forbidden_topics),
            },
        )

        for index, channel in enumerate(channels[:MAX_CHANNEL_ATTEMPTS]):
            session_name = account_sessions[index % len(account_sessions)]
            account_label = session_name or settings.telegram_session_name
            result.channels_attempted += 1

            try:
                async with TelegramAccountClient(session_name) as telegram:
                    found_post = await self._find_commentable_post(
                        channel,
                        telegram,
                        result,
                        account_label,
                    )
            except OpenAIError as error:
                logger.exception("AI topic test OpenAI request failed")
                result.errors.append(f"OpenAI: {error}")
                await self._write_log(
                    LogLevel.ERROR,
                    "ai_topic_openai_failed",
                    f"OpenAI не обработал пост из {channel.username}: {error}",
                    "channel",
                    channel.id,
                    payload={
                        "exception_type": type(error).__name__,
                        "channel_username": channel.username,
                        "account": account_label,
                        "model": settings.openai_model,
                    },
                )
                return result
            except (RPCError, ValueError, RuntimeError) as error:
                logger.warning("AI topic test skipped channel %s: %s", channel.username, error)
                result.errors.append(f"{channel.username}: {error}")
                if is_missing_username_error(error):
                    result.broken_channels += 1
                    await self._disable_channel(
                        channel.id,
                        channel.username,
                        str(error),
                    )
                await self._write_log(
                    LogLevel.WARNING,
                    "ai_topic_test_channel_skipped",
                    f"Канал {channel.username} пропущен в тесте ИИ: {error}",
                    "channel",
                    channel.id,
                    payload={
                        "exception_type": type(error).__name__,
                        "account": account_label,
                    },
                )
                continue

            if session_name is not None:
                async with async_session_factory() as session:
                    await TelegramAccountRepository(session).mark_used(account_ids[session_name])
                    await session.commit()

            if found_post is not None:
                result.post = found_post
                return result

        if result.post is None:
            result.errors.append(
                f"Не найден пост с текстом от {MIN_AI_TEST_TEXT_CHARS} символов "
                "и открытыми комментариями."
            )
            await self._write_log(
                LogLevel.WARNING,
                "ai_topic_test_no_post",
                (
                    "Тест ИИ не нашёл пост с достаточным текстом "
                    "и открытыми комментариями."
                ),
                payload={
                    "channels_total": result.channels_total,
                    "channels_attempted": result.channels_attempted,
                    "posts_checked": result.posts_checked,
                    "posts_reached_ai": result.posts_reached_ai,
                    "posts_without_text": result.posts_without_text,
                    "posts_too_short": result.posts_too_short,
                    "posts_comments_closed": result.posts_comments_closed,
                    "broken_channels": result.broken_channels,
                    "min_text_chars": MIN_AI_TEST_TEXT_CHARS,
                    "errors": result.errors[:10],
                },
            )
        return result

    async def _find_commentable_post(
        self,
        channel,
        telegram: TelegramAccountClient,
        result: AiTopicTestResult,
        account_label: str,
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
                result.posts_without_text += 1
                continue
            if len(text) < MIN_AI_TEST_TEXT_CHARS:
                result.posts_too_short += 1
                continue

            result.posts_checked += 1
            availability = await telegram.can_comment(channel.username, post.id, post.message_ids)
            if not availability.available:
                result.posts_comments_closed += 1
                continue

            result.posts_reached_ai += 1
            validation = await self.validator.validate(text)
            generated_comment = None
            comment_validation = None
            if validation.passed:
                generated_comment = await self.ai_service.generate_comment(text)
                comment_validation = await self.ai_service.validate_comment(
                    text,
                    generated_comment,
                )

            await self._save_post(channel, post, validation)
            await self._write_log(
                LogLevel.INFO
                if validation.passed and (comment_validation or {}).get("allowed", True)
                else LogLevel.WARNING,
                "ai_topic_test_completed",
                self._readable_ai_message(
                    channel.username,
                    post.id,
                    validation,
                    generated_comment,
                    comment_validation,
                ),
                "channel",
                channel.id,
                payload={
                    "validation": validation.to_dict(),
                    "channel_username": channel.username,
                    "post_id": post.id,
                    "post_url": self._post_url(channel.username, post.id),
                    "views_count": post.views or 0,
                    "account": account_label,
                    "model": settings.openai_model,
                    "text_chars": len(text),
                    "ai_used": validation.ai_used,
                    "validation_level": validation.level,
                    "passed": validation.passed,
                    "generated_comment": generated_comment,
                    "comment_validation": comment_validation,
                    "channels_attempted": result.channels_attempted,
                    "posts_checked": result.posts_checked,
                    "posts_reached_ai": result.posts_reached_ai,
                    "posts_without_text": result.posts_without_text,
                    "posts_too_short": result.posts_too_short,
                    "posts_comments_closed": result.posts_comments_closed,
                    "broken_channels": result.broken_channels,
                    "min_text_chars": MIN_AI_TEST_TEXT_CHARS,
                },
            )
            return AiTopicTestPost(
                channel_username=channel.username,
                telegram_post_id=post.id,
                text=text,
                views_count=post.views,
                validation=validation,
                generated_comment=generated_comment,
                comment_validation=comment_validation,
            )

        return None

    async def _save_post(self, channel, post, validation: PostValidationResult) -> None:
        async with async_session_factory() as session:
            db_post = await PostRepository(session).upsert(
                channel_id=channel.id,
                telegram_post_id=post.id,
                text=post.text,
                views_count=post.views or 0,
                status=(
                    PostStatus.READY_TO_COMMENT.value
                    if validation.passed
                    else PostStatus.FORBIDDEN_TOPIC.value
                ),
                skip_reason=None if validation.passed else validation.reason,
            )
            db_post.topic_analysis = validation.to_dict()
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

    async def _disable_channel(
        self,
        channel_id: int,
        channel_username: str,
        reason: str,
    ) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).disable(channel_id)
            await LogRepository(session).warning(
                "channel_auto_disabled",
                f"{channel_username} | отключён | {reason}",
                "channel",
                channel_id,
                payload={"reason": reason, "source": "ai_topic_test"},
            )
            await session.commit()

    def _readable_ai_message(
        self,
        channel_username: str,
        post_id: int,
        validation: PostValidationResult,
        generated_comment: str | None,
        comment_validation: dict | None,
    ) -> str:
        topic = validation.topic or validation.matched_topic or "-"
        comment = generated_comment or "-"
        allowed = "-"
        if not validation.passed:
            allowed = "нет"
        if comment_validation is not None:
            allowed = "да" if comment_validation.get("allowed") else "нет"
        return (
            f"{channel_username} | {self._post_url(channel_username, post_id)} | "
            f"тема: {topic} | комментарий: {comment} | allowed: {allowed}"
        )
