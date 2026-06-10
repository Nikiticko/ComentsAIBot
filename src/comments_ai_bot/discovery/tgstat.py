import asyncio
from dataclasses import dataclass, field
from html.parser import HTMLParser
import logging
import re
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from telethon.errors import FloodWaitError, RPCError

from comments_ai_bot.core.config import settings
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    LogRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.telegram_client.client import TelegramAccountClient

logger = logging.getLogger(__name__)

TGSTAT_BASE_URL = "https://uk.tgstat.com"
TGSTAT_CHANNEL_RATINGS_PATH = "/ratings/channels"
TGSTAT_PAGE_TIMEOUT_SECONDS = 15
TGSTAT_REQUEST_DELAY_SECONDS = 1
TGSTAT_VALIDATE_DELAY_SECONDS = 1
TGSTAT_DB_INSERT_LOG_STEP = 100
USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
USERNAME_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{5,32})(?![A-Za-z0-9_])")
TGSTAT_SERVICE_USERNAMES = {
    "@SearcheeBot",
    "@TGAlertsBot",
    "@TGStat",
    "@TGStatAPI",
    "@TGStatSupportBot",
    "@TGStat_Bot",
    "@TGStat_Chat",
    "@TGStatChatBot",
    "@tg_analytics_bot",
}


@dataclass(frozen=True)
class TgstatSource:
    url: str
    label: str


@dataclass(frozen=True)
class TgstatChannelCandidate:
    username: str
    title: str | None = None


@dataclass
class TgstatImportResult:
    target_total: int = 0
    channels_total_before: int = 0
    channels_total_after: int = 0
    sources_checked: int = 0
    pages_checked: int = 0
    pages_failed: int = 0
    candidates_found: int = 0
    channels_added: int = 0
    channels_existing: int = 0
    channels_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    added_usernames: list[str] = field(default_factory=list)


class TgstatChannelHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: dict[str, TgstatChannelCandidate] = {}
        self._current_link: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return

        href = dict(attrs).get("href")
        if href:
            self._current_link = href
            self._current_text = []
            self._add_usernames_from_text(href)

    def handle_data(self, data: str) -> None:
        self._add_usernames_from_text(data)
        if self._current_link is not None:
            self._current_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current_link is None:
            return

        title = " ".join(item for item in self._current_text if item) or None
        self._add_candidate_from_link(self._current_link, title)
        self._current_link = None
        self._current_text = []

    def _add_usernames_from_text(self, text: str) -> None:
        for match in USERNAME_IN_TEXT_RE.finditer(unquote(text)):
            self._add_candidate(f"@{match.group(1)}")

    def _add_candidate_from_link(self, href: str, title: str | None) -> None:
        parsed = urlparse(unquote(href))
        hostname = parsed.netloc.lower()
        path_parts = [part for part in parsed.path.split("/") if part]

        username = None
        if hostname in {"t.me", "telegram.me"} and path_parts:
            username = path_parts[0]
        elif path_parts and path_parts[0] == "channel" and len(path_parts) >= 2:
            username = path_parts[1]

        if username is None:
            return

        self._add_candidate(username, title)

    def _add_candidate(self, username: str, title: str | None = None) -> None:
        normalized = normalize_username(username)
        if normalized is None:
            return

        existing = self.candidates.get(normalized)
        if existing is None or (title and not existing.title):
            self.candidates[normalized] = TgstatChannelCandidate(normalized, title)


class TgstatChannelImporter:
    def __init__(
        self,
        *,
        max_pages: int | None = None,
        max_channels: int | None = None,
        target_total: int | None = None,
        concurrency: int | None = None,
        validate_channels: bool | None = None,
        categories: list[str] | None = None,
        sorts: list[str] | None = None,
    ) -> None:
        self.max_pages = max_pages or settings.tgstat_import_max_pages
        self.max_channels = max_channels or settings.tgstat_import_max_channels
        self.target_total = target_total or settings.tgstat_import_target_channels
        self.concurrency = concurrency or settings.tgstat_import_concurrency
        self.validate_channels = (
            settings.tgstat_validate_channels
            if validate_channels is None
            else validate_channels
        )
        self.categories = categories or settings.tgstat_import_categories
        self.sorts = sorts or settings.tgstat_import_sorts

    async def import_public_channels(self) -> TgstatImportResult:
        result = TgstatImportResult()
        result.target_total = self.target_total
        result.channels_total_before = await self._count_channels()
        result.channels_total_after = result.channels_total_before

        logger.info(
            "TGStat import started: target=%s current=%s max_candidates=%s "
            "max_pages=%s concurrency=%s validate=%s categories=%s sorts=%s",
            self.target_total,
            result.channels_total_before,
            self.max_channels,
            self.max_pages,
            self.concurrency,
            self.validate_channels,
            len(self.categories),
            ",".join(self.sorts),
        )

        if result.channels_total_before >= self.target_total:
            logger.info(
                "TGStat import skipped: current channel count %s already reached target %s",
                result.channels_total_before,
                self.target_total,
            )
            await self._log_result(result)
            return result

        candidates = await self._load_candidates(result)
        result.candidates_found = len(candidates)
        logger.info(
            "TGStat candidates loaded: candidates=%s pages_ok=%s pages_failed=%s sources=%s",
            result.candidates_found,
            result.pages_checked,
            result.pages_failed,
            result.sources_checked,
        )

        if not candidates:
            result.errors.append(
                "TGStat не вернул публичные username каналов."
            )
            await self._log_result(result)
            return result

        if not self.validate_channels:
            await self._add_candidates_until_target(candidates, result)
            logger.info(
                "TGStat import finished without Telegram validation: before=%s after=%s "
                "added=%s existing=%s target=%s",
                result.channels_total_before,
                result.channels_total_after,
                result.channels_added,
                result.channels_existing,
                result.target_total,
            )
            await self._log_result(result)
            return result

        session_names = await self._get_telegram_sessions()
        if not session_names:
            result.errors.append(
                "Нет активного Telegram-аккаунта "
                "для проверки каналов."
            )
            await self._log_result(result)
            return result

        async with TelegramAccountClient(session_names[0]) as telegram:
            for candidate in candidates:
                if result.channels_total_before + result.channels_added >= self.target_total:
                    break

                if await self._channel_exists(candidate.username):
                    result.channels_existing += 1
                    continue

                readable = await self._is_channel_readable(telegram, candidate, result)
                if readable is None:
                    break
                if readable:
                    await self._add_channel(candidate, result)
                else:
                    result.channels_skipped += 1

                await asyncio.sleep(TGSTAT_VALIDATE_DELAY_SECONDS)

        result.channels_total_after = await self._count_channels()
        logger.info(
            "TGStat import finished with Telegram validation: before=%s after=%s "
            "added=%s existing=%s skipped=%s target=%s",
            result.channels_total_before,
            result.channels_total_after,
            result.channels_added,
            result.channels_existing,
            result.channels_skipped,
            result.target_total,
        )
        await self._log_result(result)
        return result

    async def _load_candidates(self, result: TgstatImportResult) -> list[TgstatChannelCandidate]:
        candidates: dict[str, TgstatChannelCandidate] = {}
        sources = self._build_sources()
        result.sources_checked = len(sources)
        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [
            asyncio.create_task(self._load_page_limited(source, page, result, semaphore))
            for source in sources
            for page in range(1, self.max_pages + 1)
        ]

        for task in asyncio.as_completed(tasks):
            page_candidates = await task
            for candidate in page_candidates:
                candidates.setdefault(candidate.username, candidate)
                if len(candidates) >= self.max_channels:
                    for pending_task in tasks:
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    return list(candidates.values())

        return list(candidates.values())

    def _build_sources(self) -> list[TgstatSource]:
        sources: list[TgstatSource] = []
        for path in self._build_rating_paths():
            for sort in self.sorts:
                url = self._source_url(path, sort)
                label = f"{path}?sort={sort}"
                sources.append(TgstatSource(url, label))

        for category in self.categories:
            path = f"/{category}"
            sources.append(TgstatSource(urljoin(TGSTAT_BASE_URL, path), path))

        logger.info("TGStat sources prepared: %s", len(sources))
        return sources

    def _build_rating_paths(self) -> list[str]:
        paths = [TGSTAT_CHANNEL_RATINGS_PATH, f"{TGSTAT_CHANNEL_RATINGS_PATH}/public"]
        for category in self.categories:
            paths.append(f"{TGSTAT_CHANNEL_RATINGS_PATH}/{category}")
        return paths

    def _source_url(self, path: str, sort: str) -> str:
        return urljoin(TGSTAT_BASE_URL, f"{path}?{urlencode({'sort': sort})}")

    async def _load_page_limited(
        self,
        source: TgstatSource,
        page: int,
        result: TgstatImportResult,
        semaphore: asyncio.Semaphore,
    ) -> list[TgstatChannelCandidate]:
        async with semaphore:
            return await self._load_page(source, page, result)

    async def _load_page(
        self,
        source: TgstatSource,
        page: int,
        result: TgstatImportResult,
    ) -> list[TgstatChannelCandidate]:
        url = (
            source.url
            if page == 1
            else add_query_params(source.url, {"page": str(page)})
        )
        try:
            html = await asyncio.to_thread(fetch_text, url)
        except (OSError, URLError) as error:
            message = f"TGStat {source.label} page {page}: {error}"
            logger.warning(message)
            result.errors.append(message)
            result.pages_failed += 1
            return []

        parser = TgstatChannelHtmlParser()
        parser.feed(html)
        result.pages_checked += 1
        candidates = list(parser.candidates.values())
        logger.info(
            "TGStat page parsed: source=%s page=%s candidates=%s",
            source.label,
            page,
            len(candidates),
        )
        return candidates

    async def _count_channels(self) -> int:
        async with async_session_factory() as session:
            return await ChannelRepository(session).count()

    async def _channel_exists(self, username: str) -> bool:
        async with async_session_factory() as session:
            channel = await ChannelRepository(session).get_by_username(username)
        return channel is not None

    async def _get_telegram_sessions(self) -> list[str | None]:
        async with async_session_factory() as session:
            accounts = await TelegramAccountRepository(session).list_active()
        if accounts:
            return [account.session_name for account in accounts]
        return [None]

    async def _is_channel_readable(
        self,
        telegram: TelegramAccountClient,
        candidate: TgstatChannelCandidate,
        result: TgstatImportResult,
    ) -> bool | None:
        try:
            await telegram.fetch_recent_posts(candidate.username, limit=1)
            return True
        except FloodWaitError as error:
            seconds = getattr(error, "seconds", None)
            message = (
                "Telegram остановил проверку username"
                if seconds is None
                else f"Telegram остановил проверку username на {seconds} сек."
            )
            logger.warning("%s Последний канал: %s", message, candidate.username)
            result.errors.append(message)
            return None
        except (ValueError, RPCError) as error:
            logger.info("TGStat channel skipped %s: %s", candidate.username, error)
            return False
        except Exception as error:
            message = f"{candidate.username}: {error}"
            logger.warning("TGStat channel validation failed: %s", message)
            result.errors.append(message)
            return False

    async def _add_channel(
        self,
        candidate: TgstatChannelCandidate,
        result: TgstatImportResult,
    ) -> None:
        async with async_session_factory() as session:
            repo = ChannelRepository(session)
            existing = await repo.get_by_username(candidate.username)
            channel = await repo.add(candidate.username, candidate.title)
            await session.commit()

        if existing:
            result.channels_existing += 1
            return

        result.channels_added += 1
        result.added_usernames.append(channel.username)

    async def _add_candidates_until_target(
        self,
        candidates: list[TgstatChannelCandidate],
        result: TgstatImportResult,
    ) -> None:
        current_total = result.channels_total_before
        async with async_session_factory() as session:
            repo = ChannelRepository(session)
            for candidate in candidates:
                if current_total >= self.target_total:
                    break

                existing = await repo.get_by_username(candidate.username)
                channel = await repo.add(candidate.username, candidate.title)
                if existing:
                    result.channels_existing += 1
                    continue

                current_total += 1
                result.channels_added += 1
                result.added_usernames.append(channel.username)
                if result.channels_added % TGSTAT_DB_INSERT_LOG_STEP == 0:
                    logger.info(
                        "TGStat DB insert progress: added=%s existing=%s total=%s target=%s",
                        result.channels_added,
                        result.channels_existing,
                        current_total,
                        self.target_total,
                    )

            await session.commit()

        result.channels_total_after = current_total
        if result.channels_total_after < self.target_total:
            result.errors.append(
                "Не хватило новых TGStat username, "
                "чтобы добрать базу до цели."
            )
            logger.warning(
                "TGStat import stopped before target: after=%s target=%s "
                "candidates=%s added=%s existing=%s",
                result.channels_total_after,
                self.target_total,
                len(candidates),
                result.channels_added,
                result.channels_existing,
            )

    async def _log_result(self, result: TgstatImportResult) -> None:
        async with async_session_factory() as session:
            await LogRepository(session).info(
                "tgstat_channels_imported",
                (
                    "TGStat импорт: "
                    f"добавлено {result.channels_added}, "
                    f"уже было {result.channels_existing}, "
                    f"пропущено {result.channels_skipped}"
                ),
                payload={
                    "target_total": result.target_total,
                    "channels_total_before": result.channels_total_before,
                    "channels_total_after": result.channels_total_after,
                    "sources_checked": result.sources_checked,
                    "pages_checked": result.pages_checked,
                    "pages_failed": result.pages_failed,
                    "candidates_found": result.candidates_found,
                    "errors": result.errors[:20],
                },
            )
            await session.commit()


def fetch_text(url: str) -> str:
    request = Request(
        urljoin(TGSTAT_BASE_URL, url),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(request, timeout=TGSTAT_PAGE_TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def add_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def normalize_username(value: str) -> str | None:
    username = value.strip()
    username = username.removeprefix("https://t.me/").removeprefix("http://t.me/")
    username = username.removeprefix("t.me/")
    username = username.split("/", 1)[0].split("?", 1)[0]

    if not username.startswith("@"):
        username = f"@{username}"

    if not USERNAME_RE.fullmatch(username):
        return None
    if username in TGSTAT_SERVICE_USERNAMES:
        return None
    return username
