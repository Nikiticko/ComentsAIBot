import json
from typing import Any

from openai import AsyncOpenAI

from comments_ai_bot.core.config import settings

MAX_TOPIC_TEXT_CHARS = 2_000


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

    async def generate_comment(self, text: str) -> str:
        raise NotImplementedError
