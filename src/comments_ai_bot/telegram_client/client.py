import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re

from telethon import TelegramClient, errors
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.custom.message import Message
from telethon.tl.types import Channel, InputPeerChannel

from comments_ai_bot.core.config import settings
from comments_ai_bot.db.repositories import ChannelRepository
from comments_ai_bot.db.session import async_session_factory

logger = logging.getLogger(__name__)
COMMENT_VERIFY_ATTEMPTS = 3
COMMENT_VERIFY_DELAY_SECONDS = 2
MISSING_USERNAME_MARKERS = (
    "no user has",
    "as username",
)
MISSING_ENTITY_MARKERS = (
    "cannot find any entity corresponding",
    "nobody is using this username",
)
USERNAME_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{5,32})(?![A-Za-z0-9_])")


def is_missing_username_error(error: Exception) -> bool:
    message = str(error).casefold()
    return all(marker in message for marker in MISSING_USERNAME_MARKERS) or any(
        marker in message for marker in MISSING_ENTITY_MARKERS
    )


@dataclass(frozen=True)
class TelegramPost:
    id: int
    text: str | None
    views: int | None
    date: datetime
    grouped_id: int | None = None
    message_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CommentAvailability:
    available: bool
    reason: str | None = None
    post_id: int | None = None


@dataclass(frozen=True)
class TelegramChannelDiscoveryProfile:
    username: str
    title: str | None
    about: str | None
    recent_texts: tuple[str, ...]
    mentioned_usernames: tuple[str, ...]


class TelegramAccountClient:
    def __init__(self, session_name: str | None = None) -> None:
        if session_name is None:
            session_path = Path("data") / settings.telegram_session_name
        else:
            session_path = Path("data") / "accounts" / session_name
        session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(session_path),
            settings.telegram_api_id,
            settings.telegram_api_hash,
            proxy=settings.telegram_proxy,
        )

    async def __aenter__(self) -> "TelegramAccountClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.client.disconnect()

    async def fetch_new_posts(self, channel_username: str) -> list[dict]:
        posts = await self.fetch_recent_posts(channel_username)
        return [{"id": post.id, "text": post.text, "views": post.views} for post in posts]

    async def connect(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram-аккаунт не авторизован. Запусти: python scripts/auth_telegram.py"
            )
        me = await self.client.get_me()
        if me.bot:
            raise RuntimeError(
                "Telethon-сессия авторизована как бот. Для парсинга нужен обычный Telegram-аккаунт. "
                f"Удали data/{settings.telegram_session_name}.session и запусти python scripts/auth_telegram.py"
            )

    async def fetch_recent_posts(
        self,
        channel_username: str,
        *,
        limit: int = 100,
        hours: int | None = None,
    ) -> list[TelegramPost]:
        entity = await self._get_broadcast_channel(channel_username)
        messages: list[Message] = await self.client.get_messages(entity, limit=limit)

        min_date = None
        if hours is not None:
            min_date = datetime.now(timezone.utc) - timedelta(hours=hours)

        filtered_messages = [
            message
            for message in messages
            if not message.action and (min_date is None or message.date >= min_date)
        ]
        posts = self._collapse_grouped_messages(filtered_messages)
        logger.info(
            "Fetched recent posts from %s: raw_messages=%s logical_posts=%s",
            channel_username,
            len(filtered_messages),
            len(posts),
        )
        return posts

    async def inspect_channel_for_discovery(
        self,
        channel_username: str,
        *,
        post_limit: int = 30,
    ) -> TelegramChannelDiscoveryProfile:
        entity = await self._get_broadcast_channel(channel_username)
        full_channel = await self.client(GetFullChannelRequest(entity))
        about = getattr(full_channel.full_chat, "about", None)
        messages: list[Message] = await self.client.get_messages(entity, limit=post_limit)
        recent_texts = tuple(
            message.message
            for message in messages
            if message.message and not message.action
        )
        title = getattr(entity, "title", None)
        mentioned_usernames = self._extract_mentioned_usernames(title, about, recent_texts)

        return TelegramChannelDiscoveryProfile(
            username=channel_username,
            title=title,
            about=about,
            recent_texts=recent_texts,
            mentioned_usernames=mentioned_usernames,
        )

    def _collapse_grouped_messages(self, messages: list[Message]) -> list[TelegramPost]:
        grouped_messages: dict[int, list[Message]] = {}
        standalone_posts: list[TelegramPost] = []

        for message in messages:
            grouped_id = getattr(message, "grouped_id", None)
            if grouped_id is None:
                standalone_posts.append(self._message_to_post(message))
                continue

            grouped_messages.setdefault(grouped_id, []).append(message)

        grouped_posts = [
            self._album_to_post(grouped_id, album_messages)
            for grouped_id, album_messages in grouped_messages.items()
        ]
        return sorted([*standalone_posts, *grouped_posts], key=lambda post: post.date, reverse=True)

    def _message_to_post(self, message: Message) -> TelegramPost:
        return TelegramPost(
            id=message.id,
            text=message.message,
            views=message.views,
            date=message.date,
            grouped_id=getattr(message, "grouped_id", None),
            message_ids=(message.id,),
        )

    def _album_to_post(self, grouped_id: int, messages: list[Message]) -> TelegramPost:
        sorted_messages = sorted(messages, key=lambda message: message.id)
        canonical = self._select_album_canonical_message(sorted_messages)
        text_message = next((message for message in sorted_messages if message.message), canonical)
        views = max((message.views or 0 for message in sorted_messages), default=0)

        return TelegramPost(
            id=canonical.id,
            text=text_message.message,
            views=views,
            date=canonical.date,
            grouped_id=grouped_id,
            message_ids=tuple(message.id for message in sorted_messages),
        )

    def _select_album_canonical_message(self, messages: list[Message]) -> Message:
        return (
            next((message for message in messages if self._has_comment_thread(message)), None)
            or next((message for message in messages if message.message), None)
            or messages[0]
        )

    def _has_comment_thread(self, message: Message) -> bool:
        return bool(message.replies and getattr(message.replies, "comments", False))

    def _extract_mentioned_usernames(
        self,
        title: str | None,
        about: str | None,
        recent_texts: tuple[str, ...],
    ) -> tuple[str, ...]:
        mentions: dict[str, None] = {}
        for text in (title, about, *recent_texts):
            if not text:
                continue
            for match in USERNAME_IN_TEXT_RE.finditer(text):
                mentions[f"@{match.group(1)}"] = None
        return tuple(mentions)

    async def _get_broadcast_channel(self, channel_username: str) -> Channel | InputPeerChannel:
        cached_entity = await self._get_cached_channel_entity(channel_username)
        if cached_entity is not None:
            try:
                entity = await self.client.get_entity(cached_entity)
                if isinstance(entity, Channel) and bool(getattr(entity, "broadcast", False)):
                    return entity
            except (errors.RPCError, ValueError) as error:
                logger.warning(
                    "Cached entity failed for %s, resolving username again: %s",
                    channel_username,
                    error,
                )
                await self._mark_channel_entity_error(channel_username, str(error))

        try:
            entity = await self.client.get_entity(channel_username)
        except Exception as error:
            await self._mark_channel_entity_error(channel_username, str(error))
            raise

        if isinstance(entity, Channel) and bool(getattr(entity, "broadcast", False)):
            await self._cache_channel_entity(channel_username, entity)
            return entity

        raise ValueError(f"{channel_username} — это чат/группа, а не Telegram-канал")

    async def _get_cached_channel_entity(
        self,
        channel_username: str,
    ) -> InputPeerChannel | None:
        async with async_session_factory() as session:
            channel = await ChannelRepository(session).get_by_username(channel_username)

        if (
            channel is None
            or channel.telegram_channel_id is None
            or channel.telegram_access_hash is None
        ):
            return None

        return InputPeerChannel(
            channel_id=channel.telegram_channel_id,
            access_hash=channel.telegram_access_hash,
        )

    async def _cache_channel_entity(self, channel_username: str, entity: Channel) -> None:
        access_hash = getattr(entity, "access_hash", None)
        if access_hash is None:
            return

        async with async_session_factory() as session:
            await ChannelRepository(session).cache_entity(
                channel_username,
                telegram_channel_id=int(entity.id),
                telegram_access_hash=int(access_hash),
            )
            await session.commit()

    async def _mark_channel_entity_error(self, channel_username: str, error: str) -> None:
        async with async_session_factory() as session:
            await ChannelRepository(session).mark_entity_error(channel_username, error[:1000])
            await session.commit()

    async def can_comment(
        self,
        channel_username: str,
        post_id: int,
        candidate_post_ids: tuple[int, ...] | None = None,
    ) -> CommentAvailability:
        entity = await self._get_broadcast_channel(channel_username)
        post_ids = candidate_post_ids or (post_id,)
        last_reason = "Комментарии недоступны"

        for candidate_post_id in post_ids:
            message = await self.client.get_messages(entity, ids=candidate_post_id)
            if message is None:
                last_reason = "Пост не найден"
                continue

            try:
                await self.client._get_comment_data(entity, candidate_post_id)
                return CommentAvailability(True, post_id=candidate_post_id)
            except errors.ChatAdminRequiredError:
                return CommentAvailability(False, "Нет доступа к группе обсуждений")
            except errors.ChannelPrivateError:
                return CommentAvailability(False, "Группа обсуждений недоступна")
            except errors.UserBannedInChannelError:
                return CommentAvailability(False, "Аккаунт ограничен в группе обсуждений")
            except errors.RPCError as error:
                last_reason = f"Ошибка проверки комментариев: {error}"
            except (ValueError, StopIteration, AttributeError) as error:
                last_reason = (
                    "Комментарии у поста не включены"
                    if not self._has_comment_thread(message)
                    else f"Обсуждение недоступно: {error}"
                )

        return CommentAvailability(False, last_reason)

    async def send_comment(
        self,
        channel_username: str,
        post_id: int,
        text: str,
        candidate_post_ids: tuple[int, ...] | None = None,
    ) -> int:
        entity = await self._get_broadcast_channel(channel_username)
        post_ids = candidate_post_ids or (post_id,)
        last_error: Exception | None = None

        for candidate_post_id in post_ids:
            try:
                message = await self.client.send_message(
                    entity,
                    text,
                    comment_to=candidate_post_id,
                )
                await self._verify_sent_comment(message, text)
                return message.id
            except (
                errors.ChatAdminRequiredError,
                errors.ChatWriteForbiddenError,
                errors.UserBannedInChannelError,
            ):
                raise
            except errors.MsgIdInvalidError as error:
                last_error = error
            except (errors.RPCError, ValueError) as error:
                last_error = error

        if last_error is not None:
            raise last_error
        raise RuntimeError("Не найден post_id для отправки комментария")

    async def _verify_sent_comment(self, message: Message, expected_text: str) -> None:
        for _ in range(COMMENT_VERIFY_ATTEMPTS):
            await asyncio.sleep(COMMENT_VERIFY_DELAY_SECONDS)
            saved_message = await self.client.get_messages(message.peer_id, ids=message.id)
            if saved_message and saved_message.message == expected_text:
                return

        raise RuntimeError("Комментарий отправлен, но не найден при проверке")
