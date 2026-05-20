from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from typing import Any

from comments_ai_bot.core.types import LogLevel
from comments_ai_bot.db.models import Channel, Log, Post, TelegramAccount

MAX_TELEGRAM_ACCOUNTS = 100


class ChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, username: str, title: str | None = None) -> Channel:
        existing = await self.get_by_username(username)
        if existing:
            existing.title = title or existing.title
            existing.is_active = True
            return existing

        channel = Channel(username=username, title=title)
        self.session.add(channel)
        await self.session.flush()
        return channel

    async def get_by_username(self, username: str) -> Channel | None:
        result = await self.session.execute(select(Channel).where(Channel.username == username))
        return result.scalar_one_or_none()

    async def get(self, channel_id: int) -> Channel | None:
        return await self.session.get(Channel, channel_id)

    async def list_all(self) -> list[Channel]:
        result = await self.session.execute(select(Channel).order_by(Channel.id.desc()))
        return list(result.scalars().all())

    async def list_active(self) -> list[Channel]:
        result = await self.session.execute(
            select(Channel).where(Channel.is_active.is_(True)).order_by(Channel.id)
        )
        return list(result.scalars().all())

    async def toggle(self, channel_id: int) -> Channel | None:
        channel = await self.session.get(Channel, channel_id)
        if channel is None:
            return None
        channel.is_active = not channel.is_active
        await self.session.flush()
        return channel

    async def delete(self, channel_id: int) -> bool:
        channel = await self.session.get(Channel, channel_id)
        if channel is None:
            return False
        await self.session.delete(channel)
        return True


class LogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        level: LogLevel,
        event: str,
        message: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Log:
        log = Log(
            level=level.value,
            event=event,
            message=message,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )
        self.session.add(log)
        await self.session.flush()
        return log

    async def info(
        self,
        event: str,
        message: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Log:
        return await self.create(LogLevel.INFO, event, message, entity_type, entity_id, payload)

    async def warning(
        self,
        event: str,
        message: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Log:
        return await self.create(LogLevel.WARNING, event, message, entity_type, entity_id, payload)

    async def error(
        self,
        event: str,
        message: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Log:
        return await self.create(LogLevel.ERROR, event, message, entity_type, entity_id, payload)

    async def latest(self, limit: int = 10) -> list[Log]:
        result = await self.session.execute(select(Log).order_by(Log.id.desc()).limit(limit))
        return list(result.scalars().all())


class TelegramAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(TelegramAccount))
        return int(result.scalar_one())

    async def create_pending(self, session_name: str) -> TelegramAccount:
        if await self.count() >= MAX_TELEGRAM_ACCOUNTS:
            raise RuntimeError(f"Нельзя добавить больше {MAX_TELEGRAM_ACCOUNTS} Telegram-аккаунтов.")

        account = TelegramAccount(session_name=session_name, status="pending", is_active=False)
        self.session.add(account)
        await self.session.flush()
        return account

    async def mark_authorized(
        self,
        account_id: int,
        *,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        phone: str | None,
    ) -> TelegramAccount | None:
        account = await self.get(account_id)
        if account is None:
            return None

        account.telegram_user_id = telegram_user_id
        account.username = username
        account.first_name = first_name
        account.phone = phone
        account.is_active = True
        account.status = "active"
        account.last_error = None
        await self.session.flush()
        return account

    async def mark_error(self, account_id: int, error: str) -> TelegramAccount | None:
        account = await self.get(account_id)
        if account is None:
            return None

        account.status = "error"
        account.is_active = False
        account.last_error = error
        await self.session.flush()
        return account

    async def get(self, account_id: int) -> TelegramAccount | None:
        return await self.session.get(TelegramAccount, account_id)

    async def list_all(self) -> list[TelegramAccount]:
        result = await self.session.execute(select(TelegramAccount).order_by(TelegramAccount.id.desc()))
        return list(result.scalars().all())

    async def get_active(self) -> TelegramAccount | None:
        result = await self.session.execute(
            select(TelegramAccount)
            .where(TelegramAccount.is_active.is_(True), TelegramAccount.status == "active")
            .order_by(TelegramAccount.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def toggle(self, account_id: int) -> TelegramAccount | None:
        account = await self.get(account_id)
        if account is None:
            return None
        if account.status != "active":
            return account

        account.is_active = not account.is_active
        await self.session.flush()
        return account

    async def delete(self, account_id: int) -> TelegramAccount | None:
        account = await self.get(account_id)
        if account is None:
            return None

        await self.session.delete(account)
        return account


class PostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        channel_id: int,
        telegram_post_id: int,
        text: str | None,
        views_count: int | None,
        status: str,
        skip_reason: str | None = None,
    ) -> Post:
        result = await self.session.execute(
            select(Post).where(
                Post.channel_id == channel_id,
                Post.telegram_post_id == telegram_post_id,
            )
        )
        post = result.scalar_one_or_none()

        if post is None:
            post = Post(
                channel_id=channel_id,
                telegram_post_id=telegram_post_id,
                text=text,
                views_count=views_count,
                status=status,
                skip_reason=skip_reason,
            )
            self.session.add(post)
        else:
            post.text = text
            post.views_count = views_count
            post.status = status
            post.skip_reason = skip_reason

        await self.session.flush()
        return post
