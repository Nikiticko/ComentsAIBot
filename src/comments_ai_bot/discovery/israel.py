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
DISCOVERY_PROGRESS_LOG_STEP = 25
DISCOVERY_QUEUE_LOG_STEP = 50


@dataclass(frozen=True)
class DiscoveryCandidate:
    username: str
    depth: int


@dataclass
class IsraelChannelDiscoveryResult:
    target_total: int = 0
    channels_total_before: int = 0
    channels_total_after: int = 0
    seed_channels: int = 0
    search_queries: int = 0
    search_candidates: int = 0
    scanned_channels: int = 0
    matched_channels: int = 0
    discovered_mentions: int = 0
    forwarded_mentions: int = 0
    max_depth: int = 0
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
        search_limit: int | None = None,
        max_depth: int | None = None,
        seed_channels: list[str] | None = None,
        search_queries: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> None:
        self.target_total = target_total or settings.israel_discovery_target_channels
        self.max_scanned_channels = (
            max_scanned_channels or settings.israel_discovery_max_scanned_channels
        )
        self.post_limit = post_limit or settings.israel_discovery_post_limit
        self.search_limit = search_limit or settings.israel_discovery_search_limit
        self.max_depth = max_depth if max_depth is not None else settings.israel_discovery_max_depth
        self.seed_channels = seed_channels or settings.israel_discovery_seed_channels
        self.search_queries = search_queries or settings.israel_discovery_search_queries
        self.keywords = keywords if keywords is not None else settings.tgstat_import_keywords

    async def discover(self) -> IsraelChannelDiscoveryResult:
        result = IsraelChannelDiscoveryResult(
            target_total=self.target_total,
            max_depth=self.max_depth,
        )
        result.channels_total_before = await self._count_channels()
        result.channels_total_after = result.channels_total_before

        logger.info(
            "Israel channel discovery started: target=%s current=%s seeds=%s "
            "max_scanned=%s post_limit=%s search_queries=%s search_limit=%s max_depth=%s keywords=%s",
            self.target_total,
            result.channels_total_before,
            len(self.seed_channels),
            self.max_scanned_channels,
            self.post_limit,
            len(self.search_queries),
            self.search_limit,
            self.max_depth,
            len(self.keywords),
        )

        if result.channels_total_before >= self.target_total:
            result.stopped_reason = "Целевой размер базы уже достигнут."
            logger.info(
                "Israel channel discovery skipped: current=%s target=%s",
                result.channels_total_before,
                self.target_total,
            )
            await self._log_result(result)
            return result

        session_names = await self._get_telegram_sessions()
        if not session_names:
            result.stopped_reason = "Нет активного авторизованного TG-аккаунта для поиска каналов."
            result.errors.append(result.stopped_reason)
            logger.warning("Israel channel discovery stopped: no active authorized TG account")
            await self._log_result(result)
            return result

        queue, seen = self._build_initial_queue()
        result.seed_channels = len(queue)
        result.search_queries = len(self.search_queries)
        logger.info(
            "Israel discovery initial queue prepared: seeds=%s accounts=%s",
            result.seed_channels,
            len(session_names),
        )

        await self._add_search_candidates(queue, seen, session_names, result)
        if not queue:
            result.stopped_reason = "Не найдены стартовые кандидаты для израильского поиска."
            result.errors.append(result.stopped_reason)
            logger.warning("Israel channel discovery stopped: no initial candidates")
            await self._log_result(result)
            return result

        account_index = 0
        while queue and result.scanned_channels < self.max_scanned_channels:
            if result.channels_total_before + result.channels_added >= self.target_total:
                result.stopped_reason = "Целевой размер базы достигнут."
                break

            candidate = queue.popleft()
            account = session_names[account_index % len(session_names)]
            account_index += 1

            logger.info(
                "Israel discovery inspecting: username=%s depth=%s queue_left=%s",
                candidate.username,
                candidate.depth,
                len(queue),
            )
            try:
                async with TelegramAccountClient(account[0]) as telegram:
                    profile = await telegram.inspect_channel_for_discovery(
                        candidate.username,
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
                    result.errors.append(f"{candidate.username}: {error}")
                logger.info("Israel discovery skipped %s: %s", candidate.username, error)
                continue

            result.scanned_channels += 1
            if not self._is_israel_profile(profile):
                result.channels_skipped += 1
                logger.info(
                    "Israel discovery rejected: username=%s depth=%s reason=no_israel_markers",
                    candidate.username,
                    candidate.depth,
                )
                continue

            result.matched_channels += 1
            await self._add_channel(profile, result)
            logger.info(
                "Israel discovery matched: username=%s depth=%s title=%s mentions=%s forwarded=%s",
                profile.username,
                candidate.depth,
                profile.title or "-",
                len(profile.mentioned_usernames),
                len(profile.forwarded_usernames),
            )

            if candidate.depth >= self.max_depth:
                continue

            for mentioned_username in profile.mentioned_usernames:
                if self._append_candidate(queue, seen, mentioned_username, candidate.depth + 1):
                    result.discovered_mentions += 1
                    self._log_queue_progress(
                        "mention",
                        result.discovered_mentions,
                        queue,
                        candidate.depth + 1,
                    )

            for forwarded_username in profile.forwarded_usernames:
                if self._append_candidate(queue, seen, forwarded_username, candidate.depth + 1):
                    result.forwarded_mentions += 1
                    self._log_queue_progress(
                        "forwarded",
                        result.forwarded_mentions,
                        queue,
                        candidate.depth + 1,
                    )

            if result.scanned_channels % DISCOVERY_PROGRESS_LOG_STEP == 0:
                logger.info(
                    "Israel discovery progress: scanned=%s matched=%s added=%s existing=%s "
                    "skipped=%s queue=%s search=%s mentions=%s forwarded=%s",
                    result.scanned_channels,
                    result.matched_channels,
                    result.channels_added,
                    result.channels_existing,
                    result.channels_skipped,
                    len(queue),
                    result.search_candidates,
                    result.discovered_mentions,
                    result.forwarded_mentions,
                )

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
            "matched=%s added=%s existing=%s skipped=%s search=%s mentions=%s "
            "forwarded=%s stop=%s",
            result.channels_total_before,
            result.channels_total_after,
            result.scanned_channels,
            result.matched_channels,
            result.channels_added,
            result.channels_existing,
            result.channels_skipped,
            result.search_candidates,
            result.discovered_mentions,
            result.forwarded_mentions,
            result.stopped_reason,
        )
        return result

    def _build_initial_queue(self) -> tuple[deque[DiscoveryCandidate], set[str]]:
        queue: deque[DiscoveryCandidate] = deque()
        seen: set[str] = set()
        for username in self.seed_channels:
            self._append_candidate(queue, seen, username, 0)
        return queue, seen

    async def _add_search_candidates(
        self,
        queue: deque[DiscoveryCandidate],
        seen: set[str],
        session_names: list[tuple[str | None, int | None]],
        result: IsraelChannelDiscoveryResult,
    ) -> None:
        if not self.search_queries:
            return

        logger.info(
            "Israel discovery Telegram Search started: queries=%s limit=%s",
            len(self.search_queries),
            self.search_limit,
        )
        account_index = 0
        for query in self.search_queries:
            account = session_names[account_index % len(session_names)]
            account_index += 1
            try:
                async with TelegramAccountClient(account[0]) as telegram:
                    usernames = await telegram.search_public_channels(
                        query,
                        limit=self.search_limit,
                    )
                if account[1] is not None:
                    await self._mark_account_used(account[1])
            except FloodWaitError as error:
                seconds = getattr(error, "seconds", None)
                message = (
                    "Telegram остановил поиск каналов"
                    if seconds is None
                    else f"Telegram остановил поиск каналов на {seconds} сек."
                )
                result.errors.append(message)
                logger.warning(message)
                return
            except (RPCError, ValueError, RuntimeError) as error:
                result.errors.append(f"search {query}: {error}")
                logger.info("Israel discovery search skipped %s: %s", query, error)
                continue

            added = 0
            for username in usernames:
                if self._append_candidate(queue, seen, username, 0):
                    result.search_candidates += 1
                    added += 1
            logger.info(
                "Israel discovery search query done: query=%s found=%s added=%s total_search_candidates=%s queue=%s",
                query,
                len(usernames),
                added,
                result.search_candidates,
                len(queue),
            )

    def _append_candidate(
        self,
        queue: deque[DiscoveryCandidate],
        seen: set[str],
        username: str,
        depth: int,
    ) -> bool:
        normalized = normalize_username(username)
        if normalized is None or normalized in seen or depth > self.max_depth:
            return False

        seen.add(normalized)
        queue.append(DiscoveryCandidate(normalized, depth))
        return True

    def _log_queue_progress(
        self,
        source: str,
        count: int,
        queue: deque[DiscoveryCandidate],
        depth: int,
    ) -> None:
        if count == 1 or count % DISCOVERY_QUEUE_LOG_STEP == 0:
            logger.info(
                "Israel discovery queue expanded: source=%s total=%s depth=%s queue=%s",
                source,
                count,
                depth,
                len(queue),
            )

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
            logger.info("Israel discovery channel already exists: username=%s", profile.username)
            return

        result.channels_added += 1
        result.added_usernames.append(channel.username)
        logger.info(
            "Israel discovery channel added: username=%s title=%s total_added=%s",
            channel.username,
            channel.title or "-",
            result.channels_added,
        )

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
                    "search_queries": result.search_queries,
                    "search_candidates": result.search_candidates,
                    "scanned_channels": result.scanned_channels,
                    "matched_channels": result.matched_channels,
                    "discovered_mentions": result.discovered_mentions,
                    "forwarded_mentions": result.forwarded_mentions,
                    "max_depth": result.max_depth,
                    "channels_added": result.channels_added,
                    "channels_existing": result.channels_existing,
                    "channels_skipped": result.channels_skipped,
                    "stopped_reason": result.stopped_reason,
                    "errors": result.errors[:20],
                },
            )
            await session.commit()
