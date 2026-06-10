import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import random
from pathlib import Path

from openai import OpenAIError
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    RPCError,
    UserBannedInChannelError,
)

from comments_ai_bot.ai.service import MIN_AI_CONTEXT_TEXT_CHARS, AiService
from comments_ai_bot.core.config import settings
from comments_ai_bot.core.types import ChannelStatus, CommentStatus, LogLevel, PostStatus
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    CommentRepository,
    LogRepository,
    PostRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.filtering.validation import PostValidator
from comments_ai_bot.monitoring.manual_scan import POST_SCAN_HOURS, POST_SCAN_LIMIT
from comments_ai_bot.telegram_client.client import (
    CommentAvailability,
    TelegramAccountClient,
    is_missing_username_error,
)

SEND_DELAY_RANGE_SECONDS = (30, 60)
MIN_POST_AGE_MINUTES = 10
RECENT_ATTEMPT_HOURS = 24
AUTOMATION_CHANNEL_ATTEMPT_LIMIT = settings.ai_comment_automation_channel_attempt_limit
CHANNEL_COOLDOWN_HOURS = {
    "deleted_after_send": 24,
    "invalid_discussion_post": 6,
}
RECENT_PROCESSED_POST_STATUSES = {
    PostStatus.SKIPPED.value,
    PostStatus.COMMENTS_CLOSED.value,
    PostStatus.FORBIDDEN_TOPIC.value,
    PostStatus.COMMENT_GENERATED.value,
    PostStatus.PUBLISHED.value,
    PostStatus.PUBLISH_FAILED.value,
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiCommentItem:
    post_url: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class ClassifiedSendError:
    code: str
    message: str
    cooldown_hours: int | None = None
    account_level: bool = False


@dataclass
class AiCommentResult:
    channel_username: str | None = None
    account: str | None = None
    channels_total: int = 0
    channels_processed: int = 0
    posts_found: int = 0
    posts_checked: int = 0
    posts_reached_ai: int = 0
    posts_without_text: int = 0
    posts_too_short: int = 0
    posts_comments_closed: int = 0
    broken_channels: int = 0
    ai_rejected_posts: int = 0
    ai_rejected_comments: int = 0
    comments_sent: int = 0
    comments_failed: int = 0
    comments_skipped: int = 0
    account_cooldown_until: datetime | None = None
    account_cooldown_reason: str | None = None
    stopped_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    items: list[AiCommentItem] = field(default_factory=list)


class AiCommentSender:
    def __init__(
        self,
        *,
        send_delay_range_seconds: tuple[int, int] = SEND_DELAY_RANGE_SECONDS,
        ai_service: AiService | None = None,
    ) -> None:
        self.send_delay_range_seconds = send_delay_range_seconds
        self.ai_service = ai_service or AiService()
        self.validator = PostValidator(self.ai_service)

    async def send_one_per_channel(self) -> AiCommentResult:
        result = AiCommentResult()

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
        current_account_id: int | None = None
        result.account = ", ".join(
            session or settings.telegram_session_name for session in account_sessions
        )

        try:
            for index, channel in enumerate(channels):
                session_name = account_sessions[index % len(account_sessions)]
                current_account_id = account_ids.get(session_name)
                result.channel_username = channel.username

                async with TelegramAccountClient(session_name) as telegram:
                    should_continue = await self._send_one_to_channel(
                        channel,
                        telegram,
                        result,
                        account_id=current_account_id,
                    )

                result.channels_processed += 1
                if session_name is not None:
                    async with async_session_factory() as session:
                        await TelegramAccountRepository(session).mark_used(
                            account_ids[session_name]
                        )
                        await session.commit()

                if not should_continue:
                    break
        except FloodWaitError as error:
            logger.exception("AI comment sending hit Telegram flood wait")
            await self._handle_account_flood_wait(
                current_account_id,
                error,
                result,
            )
        except (RPCError, ValueError, RuntimeError) as error:
            logger.exception("AI comment sending failed")
            result.errors.append(str(error))
            message = f"ИИ-отправка не выполнена: {error}"
            await self._write_log(
                LogLevel.ERROR,
                "ai_comments_failed",
                message,
            )

        return result

    async def send_one_for_account(
        self,
        *,
        session_name: str | None,
        account_id: int | None,
        excluded_channel_ids: set[int] | None = None,
        candidate_channel_ids: set[int] | None = None,
        max_channels_attempted: int = AUTOMATION_CHANNEL_ATTEMPT_LIMIT,
    ) -> AiCommentResult:
        result = AiCommentResult(account=session_name or settings.telegram_session_name)
        excluded_channel_ids = excluded_channel_ids or set()

        async with async_session_factory() as session:
            channels = await ChannelRepository(session).list_active()
            result.channels_total = len(channels)

        channels = [channel for channel in channels if channel.id not in excluded_channel_ids]
        if candidate_channel_ids is not None:
            channels = [channel for channel in channels if channel.id in candidate_channel_ids]
        if not channels:
            result.stopped_reason = "Нет каналов без успешного комментария за сегодня."
            return result

        random.shuffle(channels)
        attempted_channels = 0

        try:
            async with TelegramAccountClient(session_name) as telegram:
                for channel in channels:
                    if attempted_channels >= max_channels_attempted:
                        result.stopped_reason = (
                            f"Достигнут лимит каналов за цикл: {max_channels_attempted}."
                        )
                        break

                    sent_before = result.comments_sent
                    result.channel_username = channel.username
                    attempted_channels += 1
                    should_continue = await self._send_one_to_channel(
                        channel,
                        telegram,
                        result,
                        account_id=account_id,
                    )
                    result.channels_processed += 1

                    if not should_continue:
                        break
                    if result.comments_sent > sent_before:
                        break

            if account_id is not None and result.comments_sent:
                async with async_session_factory() as session:
                    await TelegramAccountRepository(session).mark_used(account_id)
                    await session.commit()
        except FloodWaitError as error:
            logger.exception("Automated AI comment sending hit Telegram flood wait")
            await self._handle_account_flood_wait(account_id, error, result)
        except (RPCError, ValueError, RuntimeError) as error:
            logger.exception("Automated AI comment sending failed")
            result.errors.append(str(error))
            await self._write_log(
                LogLevel.ERROR,
                "ai_comments_automated_failed",
                f"Авторассылка не выполнена для {result.account}: {error}",
            )

        return result

    async def _send_one_to_channel(
        self,
        channel,
        telegram: TelegramAccountClient,
        result: AiCommentResult,
        *,
        account_id: int | None = None,
    ) -> bool:
        await self._mark_channel_checked(channel.id)
        try:
            posts = await telegram.fetch_recent_posts(
                channel.username,
                limit=POST_SCAN_LIMIT,
                hours=POST_SCAN_HOURS,
            )
        except FloodWaitError:
            raise
        except ValueError as error:
            result.comments_skipped += 1
            result.items.append(AiCommentItem(channel.username, "skipped", str(error)))
            if is_missing_username_error(error):
                result.broken_channels += 1
                await self._mark_channel_status(
                    channel.id,
                    channel.username,
                    str(error),
                    ChannelStatus.BAD_USERNAME,
                    source="ai_comments",
                )
            else:
                await self._mark_channel_failure(channel.id, str(error))
            await self._write_log(
                LogLevel.WARNING,
                "ai_comment_channel_skipped",
                f"Канал {channel.username} пропущен: {error}",
                "channel",
                channel.id,
            )
            return True

        random.shuffle(posts)
        posts = self._filter_mature_posts(posts)
        result.posts_found += len(posts)
        await self._add_channel_posts_checked(channel.id, len(posts))
        attempted_post_ids = await self._get_recent_attempted_post_ids(channel.id)

        for post in posts:
            if post.id in attempted_post_ids:
                result.comments_skipped += 1
                result.items.append(
                    AiCommentItem(
                        self._post_url(channel.username, post.id),
                        "skipped",
                        "Пост уже проверялся за последние 24 часа",
                    )
                )
                continue

            send_status = await self._send_to_post(
                channel,
                post,
                telegram,
                result,
                account_id=account_id,
            )
            if send_status == "sent":
                return True
            if send_status == "done":
                return True
            if send_status == "stop":
                return False

        result.items.append(
            AiCommentItem(
                channel.username,
                "skipped",
                "Нет доступного поста для комментария",
            )
        )
        return True

    async def _send_to_post(
        self,
        channel,
        post,
        telegram: TelegramAccountClient,
        result: AiCommentResult,
        *,
        account_id: int | None = None,
    ) -> str:
        post_url = self._post_url(channel.username, post.id)
        post_text = (post.text or "").strip()
        if not post_text:
            result.posts_without_text += 1
            result.comments_skipped += 1
            result.items.append(AiCommentItem(post_url, "skipped", "Пост без текста"))
            await self._save_post(
                channel,
                post,
                PostStatus.SKIPPED.value,
                "Пост без текста",
            )
            return "skip"
        if len(post_text) < MIN_AI_CONTEXT_TEXT_CHARS:
            result.posts_too_short += 1
            result.comments_skipped += 1
            reason = f"Текст короче {MIN_AI_CONTEXT_TEXT_CHARS} символов"
            result.items.append(AiCommentItem(post_url, "skipped", reason))
            await self._add_channel_too_short(channel.id)
            await self._save_post(
                channel,
                post,
                PostStatus.SKIPPED.value,
                reason,
            )
            return "skip"

        result.posts_checked += 1
        availability = await self._check_comments(channel.username, post, telegram)

        if not availability.available:
            result.posts_comments_closed += 1
            result.comments_skipped += 1
            result.items.append(AiCommentItem(post_url, "skipped", availability.reason))
            await self._mark_channel_comments_closed(
                channel.id,
                availability.reason or "Комментарии недоступны",
            )
            await self._save_post(
                channel,
                post,
                PostStatus.COMMENTS_CLOSED.value,
                availability.reason,
            )
            return "skip"

        db_post_id = await self._save_post(channel, post, PostStatus.READY_TO_COMMENT.value)
        try:
            result.posts_reached_ai += 1
            validation = await self.validator.validate(post_text)
            await self._save_post(
                channel,
                post,
                (
                    PostStatus.READY_TO_COMMENT.value
                    if validation.passed
                    else PostStatus.FORBIDDEN_TOPIC.value
                ),
                None if validation.passed else validation.reason,
                topic_analysis=validation.to_dict(),
            )
            if not validation.passed:
                result.ai_rejected_posts += 1
                result.comments_skipped += 1
                result.items.append(AiCommentItem(post_url, "skipped", validation.reason))
                await self._write_log(
                    LogLevel.WARNING,
                    "ai_post_rejected",
                    self._readable_ai_message(
                        channel.username,
                        post_url,
                        validation.topic or validation.matched_topic,
                        None,
                        False,
                    ),
                    "post",
                    db_post_id,
                    payload={
                        "post_url": post_url,
                        "validation": validation.to_dict(),
                        "model": settings.openai_model,
                    },
                )
                return "skip"

            comment_text = await self.ai_service.generate_comment(post_text)
            comment_validation = await self.ai_service.validate_comment(post_text, comment_text)
            await self._save_comment(
                db_post_id,
                CommentStatus.GENERATED.value,
                comment_text,
                error_message=None
                if comment_validation["allowed"]
                else comment_validation.get("reason"),
            )
            await self._save_post(
                channel,
                post,
                PostStatus.COMMENT_GENERATED.value,
                topic_analysis=validation.to_dict(),
            )
            await self._write_log(
                LogLevel.INFO if comment_validation["allowed"] else LogLevel.WARNING,
                "ai_comment_generated",
                self._readable_ai_message(
                    channel.username,
                    post_url,
                    validation.topic,
                    comment_text,
                    bool(comment_validation["allowed"]),
                ),
                "post",
                db_post_id,
                payload={
                    "post_url": post_url,
                    "post_topic": validation.topic,
                    "post_reason": validation.reason,
                    "post_validation": validation.to_dict(),
                    "generated_comment": comment_text,
                    "comment_validation": comment_validation,
                    "model": settings.openai_model,
                },
            )
        except (OpenAIError, ValueError, RuntimeError) as error:
            logger.exception("AI comment generation failed for %s/%s", channel.username, post.id)
            result.comments_failed += 1
            result.items.append(AiCommentItem(post_url, "failed", f"ИИ: {error}"))
            await self._write_log(
                LogLevel.ERROR,
                "ai_comment_failed",
                (
                    "ИИ не сгенерировал комментарий для "
                    f"{post_url}: {error}"
                ),
                "post",
                db_post_id,
                payload={"exception_type": type(error).__name__, "model": settings.openai_model},
            )
            return "done"

        if not comment_validation["allowed"]:
            result.ai_rejected_comments += 1
            result.comments_skipped += 1
            result.items.append(
                AiCommentItem(post_url, "skipped", comment_validation.get("reason"))
            )
            return "skip"

        comment_post_ids = (availability.post_id or post.id,)

        try:
            await self._sleep_before_send()
            telegram_comment_id = await telegram.send_comment(
                channel.username,
                post.id,
                comment_text,
                comment_post_ids,
            )
        except (
            ChatWriteForbiddenError,
            UserBannedInChannelError,
            FloodWaitError,
            RPCError,
            ValueError,
            RuntimeError,
        ) as error:
            classified_error = self._classify_send_error(error)
            logger.warning(
                "Failed to send AI comment to %s/%s: %s",
                channel.username,
                post.id,
                classified_error.message,
            )
            result.comments_failed += 1
            result.items.append(AiCommentItem(post_url, "failed", classified_error.message))
            await self._save_comment(
                db_post_id,
                CommentStatus.FAILED.value,
                comment_text,
                error_message=classified_error.message,
            )
            await self._save_post(
                channel,
                post,
                PostStatus.PUBLISH_FAILED.value,
                classified_error.message,
                topic_analysis=validation.to_dict(),
            )
            await self._write_log(
                LogLevel.ERROR,
                "ai_comment_publish_failed",
                "Не удалось отправить ИИ-комментарий: "
                f"{post_url} {classified_error.message}",
                "post",
                db_post_id,
                payload={
                    "error_code": classified_error.code,
                    "generated_comment": comment_text,
                    "comment_validation": comment_validation,
                },
            )

            if is_missing_username_error(error):
                result.broken_channels += 1
                await self._mark_channel_status(
                    channel.id,
                    channel.username,
                    classified_error.message,
                    ChannelStatus.BAD_USERNAME,
                    source="ai_comments_publish",
                )

            if classified_error.cooldown_hours is not None:
                await self._put_channel_on_cooldown(
                    channel.id,
                    channel.username,
                    classified_error.code,
                    classified_error.message,
                    classified_error.cooldown_hours,
                )
            elif classified_error.code == ChannelStatus.WRITE_FORBIDDEN.value:
                await self._mark_channel_status(
                    channel.id,
                    channel.username,
                    classified_error.message,
                    ChannelStatus.WRITE_FORBIDDEN,
                    source="ai_comments_publish",
                )
            elif classified_error.code == ChannelStatus.NEED_JOIN.value:
                await self._mark_channel_status(
                    channel.id,
                    channel.username,
                    classified_error.message,
                    ChannelStatus.NEED_JOIN,
                    source="ai_comments_publish",
                )
            else:
                await self._mark_channel_failure(channel.id, classified_error.message)

            if classified_error.account_level:
                if isinstance(error, FloodWaitError):
                    await self._handle_account_flood_wait(account_id, error, result)
                else:
                    result.stopped_reason = classified_error.message
                return "stop"
            return "done"

        result.comments_sent += 1
        await self._mark_channel_success(channel.id)
        result.items.append(AiCommentItem(post_url, "sent"))
        await self._save_comment(
            db_post_id,
            CommentStatus.PUBLISHED.value,
            comment_text,
            telegram_comment_id=telegram_comment_id,
        )
        await self._save_post(
            channel,
            post,
            PostStatus.PUBLISHED.value,
            topic_analysis=validation.to_dict(),
        )
        await self._write_log(
            LogLevel.INFO,
            "ai_comment_sent",
            self._readable_ai_message(
                channel.username,
                post_url,
                validation.topic,
                comment_text,
                bool(comment_validation["allowed"]),
            ),
            "post",
            db_post_id,
            payload={
                "generated_comment": comment_text,
                "comment_validation": comment_validation,
            },
        )
        return "sent"

    def _filter_mature_posts(self, posts) -> list:
        min_date = datetime.now(timezone.utc) - timedelta(minutes=MIN_POST_AGE_MINUTES)
        return [post for post in posts if post.date <= min_date]

    async def _get_recent_attempted_post_ids(self, channel_id: int) -> set[int]:
        async with async_session_factory() as session:
            commented_post_ids = await CommentRepository(session).list_recent_attempted_post_ids(
                channel_id=channel_id,
                hours=RECENT_ATTEMPT_HOURS,
            )
            processed_post_ids = await PostRepository(session).list_recent_processed_post_ids(
                channel_id=channel_id,
                hours=RECENT_ATTEMPT_HOURS,
                statuses=RECENT_PROCESSED_POST_STATUSES,
            )

        return commented_post_ids | processed_post_ids

    def _post_url(self, channel_username: str, post_id: int) -> str:
        return f"https://t.me/{channel_username.removeprefix('@')}/{post_id}"

    async def _sleep_before_send(self) -> None:
        delay = random.randint(*self.send_delay_range_seconds)
        if delay <= 0:
            return
        logger.info("Waiting %s seconds before AI comment", delay)
        await asyncio.sleep(delay)

    def _classify_send_error(self, error: Exception) -> ClassifiedSendError:
        message = str(error)
        normalized = message.lower()

        if isinstance(error, FloodWaitError):
            return ClassifiedSendError(
                "flood_wait",
                message,
                account_level=True,
            )
        if isinstance(error, (ChatWriteForbiddenError, UserBannedInChannelError)):
            return ClassifiedSendError(
                ChannelStatus.WRITE_FORBIDDEN.value,
                message,
            )
        if "join the discussion group" in normalized:
            return ClassifiedSendError(
                ChannelStatus.NEED_JOIN.value,
                message,
            )
        if "не найден при проверке" in normalized:
            return ClassifiedSendError(
                "deleted_after_send",
                message,
                cooldown_hours=CHANNEL_COOLDOWN_HOURS["deleted_after_send"],
            )
        if "message id" in normalized or "msg_id" in normalized:
            return ClassifiedSendError(
                "invalid_discussion_post",
                message,
                cooldown_hours=CHANNEL_COOLDOWN_HOURS["invalid_discussion_post"],
            )

        return ClassifiedSendError("send_failed", message)

    async def _put_channel_on_cooldown(
        self,
        channel_id: int,
        channel_username: str,
        reason_code: str,
        reason: str,
        hours: int,
    ) -> None:
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        async with async_session_factory() as session:
            await ChannelRepository(session).mark_cooldown(
                channel_id,
                until=until,
                reason=reason,
            )
            await LogRepository(session).warning(
                "channel_cooldown_set",
                "Канал "
                f"{channel_username} поставлен на паузу до "
                f"{until.isoformat()}: {reason}",
                "channel",
                payload={"reason_code": reason_code, "hours": hours},
            )
            await session.commit()

    async def _handle_account_flood_wait(
        self,
        account_id: int | None,
        error: FloodWaitError,
        result: AiCommentResult,
    ) -> None:
        source = self._telegram_error_source(error)
        seconds = max(0, int(getattr(error, "seconds", 0) or 0))
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        message = (
            f"Telegram FloodWait для {result.account or 'аккаунта'}: "
            f"пауза {seconds} сек. до {until:%Y-%m-%d %H:%M UTC}; source={source}"
        )
        result.account_cooldown_until = until
        result.account_cooldown_reason = message
        result.stopped_reason = message
        result.errors.append(message)

        if account_id is None:
            await self._write_log(
                LogLevel.WARNING,
                "telegram_account_flood_wait_no_account_id",
                message,
                payload={"source": source, "seconds": seconds},
            )
            return

        async with async_session_factory() as session:
            await TelegramAccountRepository(session).mark_cooldown(
                account_id,
                until=until,
                reason=message,
                source=source,
                flood_wait_seconds=seconds,
            )
            await LogRepository(session).warning(
                "telegram_account_cooldown_set",
                message,
                "telegram_account",
                account_id,
                payload={"source": source, "seconds": seconds, "until": until.isoformat()},
            )
            await session.commit()

    def _telegram_error_source(self, error: Exception) -> str:
        request = getattr(error, "request", None)
        if request is not None:
            return type(request).__name__

        message = str(error)
        marker = "(caused by "
        if marker in message and message.endswith(")"):
            return message.rsplit(marker, 1)[-1].removesuffix(")")

        return type(error).__name__

    async def _mark_channel_status(
        self,
        channel_id: int,
        channel_username: str,
        reason: str,
        status: ChannelStatus,
        *,
        source: str,
    ) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).mark_failure(channel_id, reason, status=status)
            await LogRepository(session).warning(
                "channel_status_changed",
                f"{channel_username} | {status.value} | {reason}",
                "channel",
                channel_id,
                payload={"reason": reason, "source": source, "status": status.value},
            )
            await session.commit()

    async def _mark_channel_checked(self, channel_id: int) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).mark_checked(channel_id)
            await session.commit()

    async def _add_channel_posts_checked(self, channel_id: int, count: int) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).add_posts_checked(channel_id, count)
            await session.commit()

    async def _add_channel_too_short(self, channel_id: int) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).add_too_short(channel_id)
            await session.commit()

    async def _mark_channel_comments_closed(self, channel_id: int, reason: str) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).mark_comments_closed(channel_id, reason)
            await session.commit()

    async def _mark_channel_success(self, channel_id: int) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).mark_success(channel_id)
            await session.commit()

    async def _mark_channel_failure(self, channel_id: int, reason: str) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).mark_failure(channel_id, reason)
            await session.commit()

    def _readable_ai_message(
        self,
        channel_username: str,
        post_url: str,
        topic: str | None,
        comment: str | None,
        allowed: bool,
    ) -> str:
        return (
            f"{channel_username} | {post_url} | тема: {topic or '-'} | "
            f"комментарий: {comment or '-'} | allowed: {'да' if allowed else 'нет'}"
        )

    async def _check_comments(
        self,
        channel_username: str,
        post,
        telegram: TelegramAccountClient,
    ) -> CommentAvailability:
        try:
            return await telegram.can_comment(channel_username, post.id, post.message_ids)
        except FloodWaitError:
            raise
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
        *,
        topic_analysis: dict | None = None,
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
            if topic_analysis is not None:
                db_post.topic_analysis = topic_analysis
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
