from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path

from telethon import TelegramClient, errors
from telethon.tl.custom.message import Message

from comments_ai_bot.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramPost:
    id: int
    text: str | None
    views: int | None
    date: datetime
    grouped_id: int | None = None


@dataclass(frozen=True)
class CommentAvailability:
    available: bool
    reason: str | None = None


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
        entity = await self.client.get_entity(channel_username)
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
        )

    def _album_to_post(self, grouped_id: int, messages: list[Message]) -> TelegramPost:
        sorted_messages = sorted(messages, key=lambda message: message.id)
        canonical = sorted_messages[0]
        text_message = next((message for message in sorted_messages if message.message), canonical)
        views = max((message.views or 0 for message in sorted_messages), default=0)

        return TelegramPost(
            id=canonical.id,
            text=text_message.message,
            views=views,
            date=canonical.date,
            grouped_id=grouped_id,
        )

    async def can_comment(self, channel_username: str, post_id: int) -> CommentAvailability:
        entity = await self.client.get_entity(channel_username)
        message = await self.client.get_messages(entity, ids=post_id)
        if message is None:
            return CommentAvailability(False, "Пост не найден")

        if not message.replies or not getattr(message.replies, "comments", False):
            return CommentAvailability(False, "Комментарии у поста не включены")

        try:
            await self.client._get_comment_data(entity, post_id)
        except errors.ChatAdminRequiredError:
            return CommentAvailability(False, "Нет доступа к группе обсуждений")
        except errors.ChannelPrivateError:
            return CommentAvailability(False, "Группа обсуждений недоступна")
        except errors.UserBannedInChannelError:
            return CommentAvailability(False, "Аккаунт ограничен в группе обсуждений")
        except errors.RPCError as error:
            return CommentAvailability(False, f"Ошибка проверки комментариев: {error}")
        except (ValueError, StopIteration, AttributeError) as error:
            return CommentAvailability(False, f"Обсуждение недоступно: {error}")

        return CommentAvailability(True)

    async def send_comment(self, channel_username: str, post_id: int, text: str) -> int:
        raise NotImplementedError
