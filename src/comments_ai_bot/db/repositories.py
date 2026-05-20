from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from comments_ai_bot.core.types import LogLevel
from comments_ai_bot.db.models import Channel, Log


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

    async def info(
        self,
        event: str,
        message: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
    ) -> Log:
        log = Log(
            level=LogLevel.INFO.value,
            event=event,
            message=message,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        self.session.add(log)
        await self.session.flush()
        return log

    async def latest(self, limit: int = 10) -> list[Log]:
        result = await self.session.execute(select(Log).order_by(Log.id.desc()).limit(limit))
        return list(result.scalars().all())
