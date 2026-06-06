import asyncio
import logging
import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from comments_ai_bot.core.config import settings
from comments_ai_bot.core.types import LogLevel
from comments_ai_bot.db.models import Channel, TelegramAccount
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    CommentRepository,
    LogRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.publishing.ai_comments import (
    AUTOMATION_CHANNEL_ATTEMPT_LIMIT,
    AiCommentResult,
    AiCommentSender,
)

MAILING_INTERVAL_SECONDS = settings.mailing_interval_seconds
TELEGRAM_ACCOUNT_MIN_IDLE_SECONDS = settings.telegram_account_min_idle_seconds
TELEGRAM_MESSAGE_LIMIT = 3500
try:
    LOCAL_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    LOCAL_TZ = ZoneInfo("Europe/Kiev")
logger = logging.getLogger(__name__)


class MailingAutomation:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, bot: Bot, chat_id: int) -> bool:
        if self.is_running:
            return False

        self._task = asyncio.create_task(self._run(bot, chat_id))
        return True

    async def stop(self) -> bool:
        if not self.is_running or self._task is None:
            return False

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        return True

    async def _run(self, bot: Bot, chat_id: int) -> None:
        await self._write_log(
            LogLevel.INFO,
            "mailing_started",
            (
                "Авторассылка запущена. "
                f"Интервал цикла: {MAILING_INTERVAL_SECONDS} сек, "
                f"отдых аккаунта: {TELEGRAM_ACCOUNT_MIN_IDLE_SECONDS} сек."
            ),
        )

        try:
            while True:
                accounts = await self._get_active_accounts()
                if not accounts:
                    wait_message, sleep_seconds = await self._next_account_wait()
                    if wait_message is not None:
                        await bot.send_message(chat_id, wait_message)
                        await self._write_log(
                            LogLevel.WARNING,
                            "mailing_paused_accounts_not_ready",
                            wait_message,
                            payload={"sleep_seconds": sleep_seconds},
                        )
                        await asyncio.sleep(sleep_seconds)
                        continue

                    await bot.send_message(
                        chat_id,
                        "Рассылка остановлена: нет активных TG-аккаунтов.",
                    )
                    await self._write_log(
                        LogLevel.WARNING,
                        "mailing_stopped_no_accounts",
                        "Авторассылка остановлена: нет активных TG-аккаунтов.",
                    )
                    return

                commented_channel_ids = await self._get_today_commented_channel_ids()
                candidate_channels = await self._get_candidate_channels(commented_channel_ids)
                channel_groups = self._distribute_channels(candidate_channels, len(accounts))
                tasks = [
                    self._send_for_account(account, channel_groups[index])
                    for index, account in enumerate(accounts)
                ]
                account_results = await asyncio.gather(*tasks, return_exceptions=True)
                cycle_sent, sent_report_lines, cycle_stats = await self._handle_account_results(
                    account_results
                )

                if sent_report_lines:
                    await self._send_sent_report(bot, chat_id, sent_report_lines)

                await self._write_log(
                    LogLevel.INFO,
                    "mailing_cycle_finished",
                    self._cycle_message(cycle_sent, cycle_stats),
                    payload={
                        "accounts": len(accounts),
                        "candidate_channels": len(candidate_channels),
                        "sent": cycle_sent,
                        **cycle_stats,
                    },
                )
                await asyncio.sleep(MAILING_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            await self._write_log(
                LogLevel.INFO,
                "mailing_stopped",
                "Авторассылка остановлена вручную.",
            )
            raise
        except Exception as error:
            logger.exception("Mailing automation failed")
            await self._write_log(
                LogLevel.ERROR,
                "mailing_failed",
                f"Авторассылка упала: {error}",
                payload={"exception_type": type(error).__name__},
            )
            await bot.send_message(chat_id, f"Рассылка остановлена из-за ошибки: {error}")
            raise

    async def _get_active_accounts(self) -> list[TelegramAccount]:
        async with async_session_factory() as session:
            return await TelegramAccountRepository(session).list_mailing_ready(
                TELEGRAM_ACCOUNT_MIN_IDLE_SECONDS,
            )

    async def _get_next_cooldown_account(self) -> TelegramAccount | None:
        async with async_session_factory() as session:
            return await TelegramAccountRepository(session).get_next_cooldown_account()

    async def _get_next_throttled_account(self) -> TelegramAccount | None:
        async with async_session_factory() as session:
            return await TelegramAccountRepository(session).get_next_mailing_throttled_account(
                TELEGRAM_ACCOUNT_MIN_IDLE_SECONDS,
            )

    async def _get_candidate_channels(self, excluded_channel_ids: set[int]) -> list[Channel]:
        async with async_session_factory() as session:
            channels = await ChannelRepository(session).list_active()

        candidates = [channel for channel in channels if channel.id not in excluded_channel_ids]
        random.shuffle(candidates)
        return candidates

    def _distribute_channels(
        self,
        channels: list[Channel],
        account_count: int,
    ) -> list[set[int]]:
        groups: list[set[int]] = [set() for _ in range(account_count)]
        if account_count <= 0:
            return groups

        per_account_limit = AUTOMATION_CHANNEL_ATTEMPT_LIMIT * 3
        for index, channel in enumerate(channels):
            group = groups[index % account_count]
            if len(group) < per_account_limit:
                group.add(channel.id)

        return groups

    async def _send_for_account(
        self,
        account: TelegramAccount,
        channel_ids: set[int],
    ) -> tuple[TelegramAccount, AiCommentResult]:
        sender = AiCommentSender(send_delay_range_seconds=(0, 0))
        result = await sender.send_one_for_account(
            session_name=account.session_name,
            account_id=account.id,
            candidate_channel_ids=channel_ids,
            max_channels_attempted=AUTOMATION_CHANNEL_ATTEMPT_LIMIT,
        )
        return account, result

    async def _handle_account_results(
        self,
        account_results: list[tuple[TelegramAccount, AiCommentResult] | BaseException],
    ) -> tuple[int, list[str], dict[str, int]]:
        cycle_sent = 0
        sent_report_lines: list[str] = []
        cycle_stats = self._empty_cycle_stats()

        for item in account_results:
            if isinstance(item, BaseException):
                await self._write_log(
                    LogLevel.ERROR,
                    "mailing_account_task_failed",
                    f"Задача аккаунта упала: {item}",
                    payload={"exception_type": type(item).__name__},
                )
                continue

            account, result = item
            self._add_result_stats(cycle_stats, result)
            sent_items = [
                result_item
                for result_item in result.items
                if result_item.status == "sent"
            ]
            if sent_items:
                cycle_sent += len(sent_items)
                account_title = self._account_title(account)
                sent_report_lines.extend(
                    f"{account_title}: {sent_item.post_url}" for sent_item in sent_items
                )

            if result.errors:
                await self._write_log(
                    LogLevel.ERROR,
                    "mailing_account_failed",
                    f"Аккаунт {result.account}: {'; '.join(result.errors)}",
                )

        return cycle_sent, sent_report_lines, cycle_stats

    def _empty_cycle_stats(self) -> dict[str, int]:
        return {
            "channels_processed": 0,
            "posts_found": 0,
            "posts_checked": 0,
            "posts_reached_ai": 0,
            "posts_without_text": 0,
            "posts_too_short": 0,
            "posts_comments_closed": 0,
            "broken_channels": 0,
            "ai_rejected_posts": 0,
            "ai_rejected_comments": 0,
            "comments_skipped": 0,
            "comments_failed": 0,
        }

    def _add_result_stats(
        self,
        cycle_stats: dict[str, int],
        result: AiCommentResult,
    ) -> None:
        for key in cycle_stats:
            cycle_stats[key] += int(getattr(result, key))

    def _cycle_message(self, sent: int, stats: dict[str, int]) -> str:
        return (
            f"Цикл авторассылки завершён. Отправлено: {sent}. "
            f"Постов проверено: {stats['posts_checked']}, "
            f"дошло до ИИ: {stats['posts_reached_ai']}, "
            f"коротких: {stats['posts_too_short']}, "
            f"без текста: {stats['posts_without_text']}, "
            f"комментарии закрыты: {stats['posts_comments_closed']}, "
            f"ИИ отклонил постов: {stats['ai_rejected_posts']}, "
            f"ИИ отклонил комментариев: {stats['ai_rejected_comments']}, "
            f"битых каналов: {stats['broken_channels']}."
        )

    async def _get_today_commented_channel_ids(self) -> set[int]:
        now = datetime.now(LOCAL_TZ)
        start = datetime.combine(now.date(), time.min, LOCAL_TZ).astimezone(timezone.utc)
        end = (datetime.combine(now.date(), time.min, LOCAL_TZ) + timedelta(days=1)).astimezone(
            timezone.utc
        )

        async with async_session_factory() as session:
            return await CommentRepository(session).list_channel_ids_with_published_comments(
                created_from=start,
                created_to=end,
            )

    def _account_title(self, account: TelegramAccount) -> str:
        return account.username or account.first_name or account.session_name

    async def _next_account_wait(self) -> tuple[str | None, int]:
        wait_options: list[tuple[datetime, str]] = []
        cooldown_account = await self._get_next_cooldown_account()
        if cooldown_account is not None and cooldown_account.cooldown_until is not None:
            cooldown_until = self._as_utc(cooldown_account.cooldown_until)
            wait_options.append(
                (
                    cooldown_until,
                    (
                        "Все активные TG-аккаунты на паузе. "
                        f"Ближайший доступен: {cooldown_until:%Y-%m-%d %H:%M UTC}. "
                        f"Причина: {cooldown_account.cooldown_reason or '-'}"
                    ),
                )
            )

        throttled_account = await self._get_next_throttled_account()
        if throttled_account is not None and throttled_account.last_used_at is not None:
            ready_at = self._as_utc(throttled_account.last_used_at) + timedelta(
                seconds=TELEGRAM_ACCOUNT_MIN_IDLE_SECONDS,
            )
            wait_options.append(
                (
                    ready_at,
                    (
                        "Все активные TG-аккаунты отдыхают после прошлого цикла. "
                        f"Ближайший готов: {ready_at:%Y-%m-%d %H:%M UTC}."
                    ),
                )
            )

        if not wait_options:
            return None, MAILING_INTERVAL_SECONDS

        ready_at, message = min(wait_options, key=lambda item: item[0])
        return message, self._sleep_seconds_until(ready_at)

    def _sleep_seconds_until(self, value: datetime) -> int:
        seconds = int((value - datetime.now(timezone.utc)).total_seconds())
        return max(60, seconds)

    def _as_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _send_sent_report(
        self,
        bot: Bot,
        chat_id: int,
        report_lines: list[str],
    ) -> None:
        lines = [
            "Успешные комментарии за цикл:",
            *report_lines,
        ]
        for chunk in self._split_report(lines):
            await bot.send_message(chat_id, chunk)

    def _split_report(self, lines: list[str]) -> list[str]:
        chunks: list[str] = []
        current = ""

        for line in lines:
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= TELEGRAM_MESSAGE_LIMIT:
                current = candidate
                continue

            if current:
                chunks.append(current)
            current = line

        if current:
            chunks.append(current)

        return chunks

    async def _write_log(
        self,
        level: LogLevel,
        event: str,
        message: str,
        *,
        payload: dict | None = None,
    ) -> None:
        async with async_session_factory() as session:
            await LogRepository(session).create(level, event, message, payload=payload)
            await session.commit()

        if level == LogLevel.ERROR:
            logger.error("%s: %s", event, message)
        elif level == LogLevel.WARNING:
            logger.warning("%s: %s", event, message)
        else:
            logger.info("%s: %s", event, message)


mailing_automation = MailingAutomation()
