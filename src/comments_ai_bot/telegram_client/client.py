from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, errors
from telethon.tl.custom.message import Message

from comments_ai_bot.core.config import settings


@dataclass(frozen=True)
class TelegramPost:
    id: int
    text: str | None
    views: int | None
    date: datetime


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

        return [
            TelegramPost(id=message.id, text=message.message, views=message.views, date=message.date)
            for message in messages
            if not message.action and (min_date is None or message.date >= min_date)
        ]

    async def can_comment(self, channel_username: str, post_id: int) -> CommentAvailability:
        entity = await self.client.get_entity(channel_username)
        message = await self.client.get_messages(entity, ids=post_id)
        if message is None:
            return CommentAvailability(False, "Пост не найден")

        if not message.replies or not getattr(message.replies, "comments", False):
            return CommentAvailability(False, "Комментарии у поста не включены")

        try:
            discussion_peer, _ = await self.client._get_comment_data(entity, post_id)
            permissions = await self.client.get_permissions(discussion_peer, "me")
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

        if getattr(permissions, "send_messages", None) is False:
            return CommentAvailability(False, "Нет права писать в обсуждение")

        return CommentAvailability(True)

    async def send_comment(self, channel_username: str, post_id: int, text: str) -> int:
        raise NotImplementedError
