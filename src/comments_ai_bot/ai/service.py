import json
from typing import Any

from openai import AsyncOpenAI

from comments_ai_bot.core.config import settings

MAX_TOPIC_TEXT_CHARS = 2_000
MAX_COMMENT_TEXT_CHARS = 120


class AiService:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def analyze_topic(self, text: str) -> dict[str, Any]:
        trimmed_text = text.strip()[:MAX_TOPIC_TEXT_CHARS]
        if not trimmed_text:
            return {
                "topic": "нет текста",
                "confidence": 0,
                "reason": "Пост без текстового содержимого",
            }

        response = await self.client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": settings.ai_topic_prompt},
                {"role": "user", "content": trimmed_text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=120,
        )
        content = response.choices[0].message.content or "{}"

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = {"topic": content.strip(), "confidence": None, "reason": "Ответ не JSON"}

        return {
            "topic": str(payload.get("topic") or "не определено").strip(),
            "confidence": payload.get("confidence"),
            "reason": str(payload.get("reason") or "").strip() or None,
        }

    async def validate_forbidden_topic(
        self,
        text: str,
        forbidden_topics: list[str],
    ) -> dict[str, Any]:
        trimmed_text = text.strip()[:MAX_TOPIC_TEXT_CHARS]
        if not trimmed_text:
            return {
                "forbidden": False,
                "matched_topic": None,
                "confidence": 0,
                "reason": "Пост без текстового содержимого",
                "topic": "нет текста",
            }

        response = await self.client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": settings.ai_forbidden_topic_prompt},
                {
                    "role": "user",
                    "content": (
                        "Запрещённые темы:\n"
                        f"{', '.join(forbidden_topics)}\n\n"
                        f"Пост:\n{trimmed_text}"
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=140,
        )
        content = response.choices[0].message.content or "{}"

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = {
                "forbidden": False,
                "matched_topic": None,
                "confidence": None,
                "reason": "Ответ не JSON",
                "topic": content.strip(),
            }

        return {
            "forbidden": self._coerce_bool(payload.get("forbidden")),
            "matched_topic": payload.get("matched_topic"),
            "confidence": payload.get("confidence"),
            "reason": str(payload.get("reason") or "").strip() or None,
            "topic": str(payload.get("topic") or "не определено").strip(),
        }

    async def generate_comment(self, text: str) -> str:
        trimmed_text = text.strip()[:MAX_TOPIC_TEXT_CHARS]
        if not trimmed_text:
            raise ValueError(
                "Нельзя сгенерировать комментарий без текста поста."
            )

        response = await self.client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": settings.ai_comment_prompt},
                {"role": "user", "content": f"Пост:\n{trimmed_text}"},
            ],
            temperature=0.7,
            max_tokens=80,
        )
        comment = (response.choices[0].message.content or "").strip().strip('"')
        comment = " ".join(comment.split())
        if not comment:
            raise ValueError("OpenAI вернул пустой комментарий.")
        return comment[:MAX_COMMENT_TEXT_CHARS]

    async def validate_comment(self, post_text: str, comment_text: str) -> dict[str, Any]:
        trimmed_post = post_text.strip()[:MAX_TOPIC_TEXT_CHARS]
        trimmed_comment = comment_text.strip()[:MAX_COMMENT_TEXT_CHARS]
        if not trimmed_post or not trimmed_comment:
            return {
                "allowed": False,
                "confidence": 0,
                "reason": "Нет текста поста или комментария",
            }

        response = await self.client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": settings.ai_comment_validation_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Пост:\n{trimmed_post}\n\n"
                        f"Комментарий:\n{trimmed_comment}"
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=100,
        )
        content = response.choices[0].message.content or "{}"

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = {
                "allowed": False,
                "confidence": None,
                "reason": "Ответ проверки не JSON",
            }

        return {
            "allowed": self._coerce_bool(payload.get("allowed")),
            "confidence": payload.get("confidence"),
            "reason": str(payload.get("reason") or "").strip() or None,
        }

    def _coerce_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "да"}
        return bool(value)
