import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from comments_ai_bot.core.types import LogLevel
from comments_ai_bot.db.repositories import (
    ChannelRepository,
    CommentRepository,
    LogRepository,
    TelegramAccountRepository,
)
from comments_ai_bot.db.models import TelegramAccount
from comments_ai_bot.db.session import async_session_factory
from comments_ai_bot.publishing.test_comments import TestCommentSender

MAILING_INTERVAL_SECONDS = 30
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
            "Авторассылка запущена.",
        )

        try:
            while True:
                accounts = await self._get_active_accounts()
                if not accounts:
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
                cycle_sent = 0
                sent_report_lines: list[str] = []

                for account in accounts:
                    sender = TestCommentSender(send_delay_range_seconds=(0, 0))
                    result = await sender.send_one_for_account(
                        session_name=account.session_name,
                        account_id=account.id,
                        excluded_channel_ids=commented_channel_ids,
                    )

                    sent_items = [item for item in result.items if item.status == "sent"]
                    if sent_items and result.channel_username:
                        cycle_sent += 1
                        account_title = self._account_title(account)
                        sent_report_lines.extend(
                            f"{account_title}: {item.post_url}" for item in sent_items
                        )
                        channel_id = await self._get_channel_id(result.channel_username)
                        if channel_id is not None:
                            commented_channel_ids.add(channel_id)

                    if result.errors:
                        await self._write_log(
                            LogLevel.ERROR,
                            "mailing_account_failed",
                            f"Аккаунт {result.account}: {'; '.join(result.errors)}",
                        )

                if sent_report_lines:
                    await self._send_sent_report(bot, chat_id, sent_report_lines)

                await self._write_log(
                    LogLevel.INFO,
                    "mailing_cycle_finished",
                    f"Цикл авторассылки завершён. Отправлено: {cycle_sent}.",
                    payload={"accounts": len(accounts), "sent": cycle_sent},
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
            return await TelegramAccountRepository(session).list_active()

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

    async def _get_channel_id(self, channel_username: str) -> int | None:
        async with async_session_factory() as session:
            channel = await ChannelRepository(session).get_by_username(channel_username)
            return channel.id if channel is not None else None

    def _account_title(self, account: TelegramAccount) -> str:
        return account.username or account.first_name or account.session_name

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


mailing_automation = MailingAutomation()
