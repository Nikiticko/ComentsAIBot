from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from comments_ai_bot.core.types import CommentStatus, LogLevel
from comments_ai_bot.db.models import Channel, Comment, Log, Post, Setting, TelegramAccount

MAX_TELEGRAM_ACCOUNTS = 100


class ChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(Channel))
        return int(result.scalar_one())

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

    async def disable(self, channel_id: int) -> Channel | None:
        channel = await self.session.get(Channel, channel_id)
        if channel is None:
            return None
        channel.is_active = False
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
        account.cooldown_until = None
        account.cooldown_reason = None
        account.cooldown_source = None
        account.flood_wait_seconds = None
        await self.session.flush()
        return account

    async def upsert_authorized(
        self,
        session_name: str,
        *,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        phone: str | None,
    ) -> TelegramAccount:
        account = await self.get_by_session_name(session_name)
        if account is None and telegram_user_id:
            account = await self.get_by_telegram_user_id(telegram_user_id)

        if account is None:
            if await self.count() >= MAX_TELEGRAM_ACCOUNTS:
                raise RuntimeError(
                    f"Нельзя добавить больше {MAX_TELEGRAM_ACCOUNTS} Telegram-аккаунтов."
                )
            account = TelegramAccount(session_name=session_name)
            self.session.add(account)

        account.session_name = session_name
        account.telegram_user_id = telegram_user_id
        account.username = username
        account.first_name = first_name
        account.phone = phone
        account.is_active = True
        account.status = "active"
        account.last_error = None
        account.cooldown_until = None
        account.cooldown_reason = None
        account.cooldown_source = None
        account.flood_wait_seconds = None
        await self.session.flush()
        return account

    async def mark_error(self, account_id: int, error: str) -> TelegramAccount | None:
        account = await self.get(account_id)
        if account is None:
            return None

        account.status = "error"
        account.is_active = False
        account.last_error = error
        account.cooldown_until = None
        account.cooldown_reason = None
        account.cooldown_source = None
        account.flood_wait_seconds = None
        await self.session.flush()
        return account

    async def get(self, account_id: int) -> TelegramAccount | None:
        return await self.session.get(TelegramAccount, account_id)

    async def get_by_session_name(self, session_name: str) -> TelegramAccount | None:
        result = await self.session.execute(
            select(TelegramAccount).where(TelegramAccount.session_name == session_name)
        )
        return result.scalar_one_or_none()

    async def get_by_telegram_user_id(
        self,
        telegram_user_id: int,
    ) -> TelegramAccount | None:
        result = await self.session.execute(
            select(TelegramAccount).where(
                TelegramAccount.telegram_user_id == telegram_user_id
            )
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[TelegramAccount]:
        result = await self.session.execute(select(TelegramAccount).order_by(TelegramAccount.id.desc()))
        return list(result.scalars().all())

    async def get_active(self) -> TelegramAccount | None:
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(TelegramAccount)
            .where(
                TelegramAccount.is_active.is_(True),
                TelegramAccount.status == "active",
                self._available_cooldown_filter(now),
            )
            .order_by(TelegramAccount.last_used_at.is_not(None), TelegramAccount.last_used_at, TelegramAccount.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_active(self) -> list[TelegramAccount]:
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(TelegramAccount)
            .where(
                TelegramAccount.is_active.is_(True),
                TelegramAccount.status == "active",
                self._available_cooldown_filter(now),
            )
            .order_by(TelegramAccount.last_used_at.is_not(None), TelegramAccount.last_used_at, TelegramAccount.id)
        )
        return list(result.scalars().all())

    async def list_mailing_ready(self, min_idle_seconds: int) -> list[TelegramAccount]:
        now = datetime.now(timezone.utc)
        idle_before = now - timedelta(seconds=min_idle_seconds)
        result = await self.session.execute(
            select(TelegramAccount)
            .where(
                TelegramAccount.is_active.is_(True),
                TelegramAccount.status == "active",
                self._available_cooldown_filter(now),
                or_(
                    TelegramAccount.last_used_at.is_(None),
                    TelegramAccount.last_used_at <= idle_before,
                ),
            )
            .order_by(
                TelegramAccount.last_used_at.is_not(None),
                TelegramAccount.last_used_at,
                TelegramAccount.id,
            )
        )
        return list(result.scalars().all())

    async def list_enabled(self) -> list[TelegramAccount]:
        result = await self.session.execute(
            select(TelegramAccount)
            .where(TelegramAccount.is_active.is_(True), TelegramAccount.status == "active")
            .order_by(TelegramAccount.id)
        )
        return list(result.scalars().all())

    async def get_next_cooldown_account(self) -> TelegramAccount | None:
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(TelegramAccount)
            .where(
                TelegramAccount.is_active.is_(True),
                TelegramAccount.status == "active",
                TelegramAccount.cooldown_until.is_not(None),
                TelegramAccount.cooldown_until > now,
            )
            .order_by(TelegramAccount.cooldown_until, TelegramAccount.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_next_mailing_throttled_account(
        self,
        min_idle_seconds: int,
    ) -> TelegramAccount | None:
        now = datetime.now(timezone.utc)
        idle_before = now - timedelta(seconds=min_idle_seconds)
        result = await self.session.execute(
            select(TelegramAccount)
            .where(
                TelegramAccount.is_active.is_(True),
                TelegramAccount.status == "active",
                self._available_cooldown_filter(now),
                TelegramAccount.last_used_at.is_not(None),
                TelegramAccount.last_used_at > idle_before,
            )
            .order_by(TelegramAccount.last_used_at, TelegramAccount.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def mark_used(self, account_id: int) -> TelegramAccount | None:
        account = await self.get(account_id)
        if account is None:
            return None

        account.last_used_at = datetime.now(timezone.utc)
        account.usage_count += 1
        cooldown_until = self._as_utc(account.cooldown_until)
        if cooldown_until is not None and cooldown_until <= account.last_used_at:
            account.cooldown_until = None
            account.cooldown_reason = None
            account.cooldown_source = None
            account.flood_wait_seconds = None
        await self.session.flush()
        return account

    async def mark_cooldown(
        self,
        account_id: int,
        *,
        until: datetime,
        reason: str,
        source: str,
        flood_wait_seconds: int | None = None,
    ) -> TelegramAccount | None:
        account = await self.get(account_id)
        if account is None:
            return None

        account.cooldown_until = until
        account.cooldown_reason = reason
        account.cooldown_source = source
        account.flood_wait_seconds = flood_wait_seconds
        account.last_error = reason
        await self.session.flush()
        return account

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

    def _available_cooldown_filter(self, now: datetime):
        return or_(
            TelegramAccount.cooldown_until.is_(None),
            TelegramAccount.cooldown_until <= now,
        )

    def _as_utc(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


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

    async def list_by_status(self, status: str, limit: int = 50) -> list[tuple[Post, Channel]]:
        result = await self.session.execute(
            select(Post, Channel)
            .join(Channel, Channel.id == Post.channel_id)
            .where(Post.status == status)
            .order_by(Post.views_count.desc(), Post.id.desc())
            .limit(limit)
        )
        return list(result.all())


class CommentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        post_id: int,
        text: str,
        status: str,
        telegram_comment_id: int | None = None,
        error_message: str | None = None,
    ) -> Comment:
        comment = Comment(
            post_id=post_id,
            text=text,
            status=status,
            telegram_comment_id=telegram_comment_id,
            error_message=error_message,
        )
        self.session.add(comment)
        await self.session.flush()
        return comment

    async def list_recent_attempted_post_ids(
        self,
        *,
        channel_id: int,
        hours: int = 24,
    ) -> set[int]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await self.session.execute(
            select(Post.telegram_post_id)
            .join(Comment, Comment.post_id == Post.id)
            .where(Post.channel_id == channel_id, Comment.created_at >= since)
        )
        return set(result.scalars().all())

    async def list_channel_ids_with_published_comments(
        self,
        *,
        created_from: datetime,
        created_to: datetime,
    ) -> set[int]:
        result = await self.session.execute(
            select(distinct(Post.channel_id))
            .join(Comment, Comment.post_id == Post.id)
            .where(
                Comment.status == CommentStatus.PUBLISHED.value,
                Comment.created_at >= created_from,
                Comment.created_at < created_to,
            )
        )
        return set(result.scalars().all())


class SettingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_value(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self.session.execute(select(Setting).where(Setting.key == key))
        setting = result.scalar_one_or_none()
        if setting is None:
            return default or {}
        return setting.value or {}

    async def set_value(self, key: str, value: dict[str, Any]) -> Setting:
        result = await self.session.execute(select(Setting).where(Setting.key == key))
        setting = result.scalar_one_or_none()
        if setting is None:
            setting = Setting(key=key, value=dict(value))
            self.session.add(setting)
        else:
            setting.value = dict(value)

        await self.session.flush()
        return setting
