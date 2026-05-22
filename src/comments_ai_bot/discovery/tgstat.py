import asyncio
from dataclasses import dataclass, field
from html.parser import HTMLParser
import logging
import re
from urllib.error import URLError
from urllib.parse import urlencode, unquote, urljoin, urlparse
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
    sources_checked: int = 0
    pages_checked: int = 0
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
        categories: list[str] | None = None,
        sorts: list[str] | None = None,
    ) -> None:
        self.max_pages = max_pages or settings.tgstat_import_max_pages
        self.max_channels = max_channels or settings.tgstat_import_max_channels
        self.categories = categories or settings.tgstat_import_categories
        self.sorts = sorts or settings.tgstat_import_sorts

    async def import_public_channels(self) -> TgstatImportResult:
        result = TgstatImportResult()
        candidates = await self._load_candidates(result)
        result.candidates_found = len(candidates)

        if not candidates:
            result.errors.append(
                "TGStat не вернул публичные username каналов."
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
                readable = await self._is_channel_readable(telegram, candidate, result)
                if readable is None:
                    break
                if readable:
                    await self._add_channel(candidate, result)
                else:
                    result.channels_skipped += 1

                await asyncio.sleep(TGSTAT_VALIDATE_DELAY_SECONDS)

        await self._log_result(result)
        return result

    async def _load_candidates(self, result: TgstatImportResult) -> list[TgstatChannelCandidate]:
        candidates: dict[str, TgstatChannelCandidate] = {}
        for source in self._build_sources():
            result.sources_checked += 1
            for page in range(1, self.max_pages + 1):
                page_candidates = await self._load_page(source, page, result)
                if not page_candidates:
                    break

                result.pages_checked += 1
                for candidate in page_candidates:
                    candidates.setdefault(candidate.username, candidate)
                    if len(candidates) >= self.max_channels:
                        return list(candidates.values())

                await asyncio.sleep(TGSTAT_REQUEST_DELAY_SECONDS)

            if len(candidates) >= self.max_channels:
                break

        return list(candidates.values())

    def _build_sources(self) -> list[TgstatSource]:
        sources: list[TgstatSource] = []
        paths = [f"{TGSTAT_CHANNEL_RATINGS_PATH}/public"]
        paths.extend(
            f"{TGSTAT_CHANNEL_RATINGS_PATH}/{category}/public"
            for category in self.categories
        )

        for path in paths:
            for sort in self.sorts:
                url = self._source_url(path, sort)
                label = f"{path}?sort={sort}"
                sources.append(TgstatSource(url, label))

        return sources

    def _source_url(self, path: str, sort: str) -> str:
        return urljoin(TGSTAT_BASE_URL, f"{path}?{urlencode({'sort': sort})}")

    async def _load_page(
        self,
        source: TgstatSource,
        page: int,
        result: TgstatImportResult,
    ) -> list[TgstatChannelCandidate]:
        url = (
            source.url
            if page == 1
            else f"{source.url}&{urlencode({'page': page})}"
        )
        try:
            html = await asyncio.to_thread(fetch_text, url)
        except (OSError, URLError) as error:
            message = f"TGStat {source.label} page {page}: {error}"
            logger.warning(message)
            result.errors.append(message)
            return []

        parser = TgstatChannelHtmlParser()
        parser.feed(html)
        return list(parser.candidates.values())

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
                    "sources_checked": result.sources_checked,
                    "pages_checked": result.pages_checked,
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
