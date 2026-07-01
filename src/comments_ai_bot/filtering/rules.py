from comments_ai_bot.core.config import settings

FORBIDDEN_TOPICS = {
    "политика",
    "война",
    "трагедии",
    "религия",
    "медицина",
    "смерть",
    "теракты",
    "катастрофы",
    "преступления",
    "дети в опасных ситуациях",
    "мобилизация",
    "национальные конфликты",
    "похороны",
    "тяжёлые болезни",
    "благотворительные сборы",
}

MIN_POST_VIEWS = settings.post_min_views


def format_min_post_views() -> str:
    if MIN_POST_VIEWS >= 1_000 and MIN_POST_VIEWS % 1_000 == 0:
        return f"{MIN_POST_VIEWS // 1_000}к+"
    return f"{MIN_POST_VIEWS}+"
