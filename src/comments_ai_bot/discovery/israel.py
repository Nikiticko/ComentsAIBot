from collections import deque
from dataclasses import dataclass, field
import logging
import re

from telethon.errors import FloodWaitError, RPCError

from comments_ai_bot.core.config import settings
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    LogRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.discovery.tgstat import normalize_username
from comments_ai_bot.telegram_client.client import (
    TelegramAccountClient,
    TelegramChannelDiscoveryProfile,
    is_missing_username_error,
)

logger = logging.getLogger(__name__)

HEBREW_RE = re.compile(r"[\u0590-\u05FF]")


@dataclass
class IsraelChannelDiscoveryResult:
    target_total: int = 0
    channels_total_before: int = 0
    channels_total_after: int = 0
    seed_channels: int = 0
    scanned_channels: int = 0
    matched_channels: int = 0
    discovered_mentions: int = 0
    channels_added: int = 0
    channels_existing: int = 0
    channels_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    added_usernames: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


class IsraelChannelDiscoverer:
    def __init__(
        self,
        *,
        target_total: int | None = None,
        max_scanned_channels: int | None = None,
        post_limit: int | None = None,
        seed_channels: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> None:
        self.target_total = target_total or settings.israel_discovery_target_channels
        self.max_scanned_channels = (
            max_scanned_channels or settings.israel_discovery_max_scanned_channels
        )
        self.post_limit = post_limit or settings.israel_discovery_post_limit
        self.seed_channels = seed_channels or settings.israel_discovery_seed_channels
        self.keywords = keywords if keywords is not None else settings.tgstat_import_keywords

    async def discover(self) -> IsraelChannelDiscoveryResult:
        result = IsraelChannelDiscoveryResult(target_total=self.target_total)
        result.channels_total_before = await self._count_channels()
        result.channels_total_after = result.channels_total_before

        logger.info(
            "Israel channel discovery started: target=%s current=%s seeds=%s "
            "max_scanned=%s post_limit=%s keywords=%s",
            self.target_total,
            result.channels_total_before,
            len(self.seed_channels),
            self.max_scanned_channels,
            self.post_limit,
            len(self.keywords),
        )

        if result.channels_total_before >= self.target_total:
            result.stopped_reason = "Целевой размер базы уже достигнут."
            await self._log_result(result)
            return result

        session_names = await self._get_telegram_sessions()
        if not session_names:
            result.stopped_reason = "Нет активного авторизованного TG-аккаунта для поиска каналов."
            result.errors.append(result.stopped_reason)
            await self._log_result(result)
            return result

        queue, seen = self._build_initial_queue()
        result.seed_channels = len(queue)
        if not queue:
            result.stopped_reason = "Не заданы seed-каналы для израильского поиска."
            result.errors.append(result.stopped_reason)
            await self._log_result(result)
            return result

        account_index = 0
        while queue and result.scanned_channels < self.max_scanned_channels:
            if result.channels_total_before + result.channels_added >= self.target_total:
                result.stopped_reason = "Целевой размер базы достигнут."
                break

            username = queue.popleft()
            account = session_names[account_index % len(session_names)]
            account_index += 1

            try:
                async with TelegramAccountClient(account[0]) as telegram:
                    profile = await telegram.inspect_channel_for_discovery(
                        username,
                        post_limit=self.post_limit,
                    )
                if account[1] is not None:
                    await self._mark_account_used(account[1])
            except FloodWaitError as error:
                seconds = getattr(error, "seconds", None)
                result.stopped_reason = (
                    "Telegram остановил обнаружение каналов"
                    if seconds is None
                    else f"Telegram остановил обнаружение каналов на {seconds} сек."
                )
                result.errors.append(result.stopped_reason)
                break
            except (RPCError, ValueError, RuntimeError) as error:
                result.channels_skipped += 1
                if not is_missing_username_error(error):
                    result.errors.append(f"{username}: {error}")
                logger.info("Israel discovery skipped %s: %s", username, error)
                continue

            result.scanned_channels += 1
            if not self._is_israel_profile(profile):
                result.channels_skipped += 1
                continue

            result.matched_channels += 1
            await self._add_channel(profile, result)

            for mentioned_username in profile.mentioned_usernames:
                normalized = normalize_username(mentioned_username)
                if normalized is None or normalized in seen:
                    continue
                seen.add(normalized)
                queue.append(normalized)
                result.discovered_mentions += 1

        if result.stopped_reason is None:
            result.stopped_reason = (
                "Очередь кандидатов закончилась."
                if not queue
                else "Достигнут лимит проверенных каналов."
            )

        result.channels_total_after = await self._count_channels()
        await self._log_result(result)
        logger.info(
            "Israel channel discovery finished: before=%s after=%s scanned=%s "
            "matched=%s added=%s existing=%s skipped=%s mentions=%s stop=%s",
            result.channels_total_before,
            result.channels_total_after,
            result.scanned_channels,
            result.matched_channels,
            result.channels_added,
            result.channels_existing,
            result.channels_skipped,
            result.discovered_mentions,
            result.stopped_reason,
        )
        return result

    def _build_initial_queue(self) -> tuple[deque[str], set[str]]:
        queue: deque[str] = deque()
        seen: set[str] = set()
        for username in self.seed_channels:
            normalized = normalize_username(username)
            if normalized is None or normalized in seen:
                continue
            seen.add(normalized)
            queue.append(normalized)
        return queue, seen

    def _is_israel_profile(self, profile: TelegramChannelDiscoveryProfile) -> bool:
        text = " ".join(
            item
            for item in (
                profile.username,
                profile.title,
                profile.about,
                *profile.recent_texts,
            )
            if item
        )
        folded_text = text.casefold()
        if HEBREW_RE.search(text):
            return True
        return any(keyword in folded_text for keyword in self.keywords)

    async def _add_channel(
        self,
        profile: TelegramChannelDiscoveryProfile,
        result: IsraelChannelDiscoveryResult,
    ) -> None:
        async with async_session_factory() as session:
            repo = ChannelRepository(session)
            existing = await repo.get_by_username(profile.username)
            channel = await repo.add(profile.username, profile.title)
            await session.commit()

        if existing:
            result.channels_existing += 1
            return

        result.channels_added += 1
        result.added_usernames.append(channel.username)

    async def _count_channels(self) -> int:
        async with async_session_factory() as session:
            return await ChannelRepository(session).count()

    async def _get_telegram_sessions(self) -> list[tuple[str | None, int | None]]:
        async with async_session_factory() as session:
            accounts = await TelegramAccountRepository(session).list_discovery_ready()
        if accounts:
            return [(account.session_name, account.id) for account in accounts]
        return [(None, None)]

    async def _mark_account_used(self, account_id: int) -> None:
        async with async_session_factory() as session:
            await TelegramAccountRepository(session).mark_used(account_id)
            await session.commit()

    async def _log_result(self, result: IsraelChannelDiscoveryResult) -> None:
        async with async_session_factory() as session:
            await LogRepository(session).info(
                "israel_channels_discovered",
                (
                    "Израильский импорт: "
                    f"добавлено {result.channels_added}, "
                    f"уже было {result.channels_existing}, "
                    f"пропущено {result.channels_skipped}"
                ),
                payload={
                    "target_total": result.target_total,
                    "channels_total_before": result.channels_total_before,
                    "channels_total_after": result.channels_total_after,
                    "seed_channels": result.seed_channels,
                    "scanned_channels": result.scanned_channels,
                    "matched_channels": result.matched_channels,
                    "discovered_mentions": result.discovered_mentions,
                    "channels_added": result.channels_added,
                    "channels_existing": result.channels_existing,
                    "channels_skipped": result.channels_skipped,
                    "stopped_reason": result.stopped_reason,
                    "errors": result.errors[:20],
                },
            )
            await session.commit()
