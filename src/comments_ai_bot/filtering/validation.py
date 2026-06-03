from dataclasses import dataclass
import re

from comments_ai_bot.ai.service import AiService
from comments_ai_bot.core.config import settings


@dataclass(frozen=True)
class PostValidationResult:
    passed: bool
    level: str
    ai_used: bool
    trigger_word: str | None = None
    matched_topic: str | None = None
    confidence: float | int | str | None = None
    reason: str | None = None
    topic: str | None = None

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "level": self.level,
            "ai_used": self.ai_used,
            "trigger_word": self.trigger_word,
            "matched_topic": self.matched_topic,
            "confidence": self.confidence,
            "reason": self.reason,
            "topic": self.topic,
        }


class PostValidator:
    def __init__(self, ai_service: AiService | None = None) -> None:
        self.ai_service = ai_service

    async def validate(self, text: str) -> PostValidationResult:
        trigger_word = self._find_trigger_word(text, settings.post_trigger_words)
        if trigger_word is not None:
            return PostValidationResult(
                passed=False,
                level="trigger_word",
                ai_used=False,
                trigger_word=trigger_word,
                reason=f"Найдено триггер-слово: {trigger_word}",
            )

        ai_result = await self._ai_service().validate_forbidden_topic(
            text,
            settings.forbidden_topics,
        )
        forbidden = bool(ai_result.get("forbidden"))
        matched_topic = ai_result.get("matched_topic")
        return PostValidationResult(
            passed=not forbidden,
            level="ai_topic" if forbidden else "passed",
            ai_used=True,
            matched_topic=str(matched_topic).strip() if matched_topic else None,
            confidence=ai_result.get("confidence"),
            reason=ai_result.get("reason"),
            topic=ai_result.get("topic"),
        )

    def _find_trigger_word(self, text: str, trigger_words: list[str]) -> str | None:
        normalized_text = text.casefold()
        for word in trigger_words:
            normalized_word = word.strip().casefold()
            if not normalized_word:
                continue
            if self._contains_word(normalized_text, normalized_word):
                return word
        return None

    def _ai_service(self) -> AiService:
        if self.ai_service is None:
            self.ai_service = AiService()
        return self.ai_service

    def _contains_word(self, text: str, word: str) -> bool:
        if re.search(r"\s", word):
            return word in text

        return re.search(rf"(?<![\wа-яёіїєґ]){re.escape(word)}(?![\wа-яёіїєґ])", text) is not None
