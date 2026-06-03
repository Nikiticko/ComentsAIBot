from functools import lru_cache

from dotenv import load_dotenv

from comments_ai_bot.core.types import AppEnv

load_dotenv()


def _getenv(name: str, default: str | None = None) -> str | None:
    import os

    return os.getenv(name, default)


def _getenv_required(name: str) -> str:
    value = _getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _getenv_int_list(name: str) -> list[int]:
    value = _getenv(name, "")
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _getenv_str_list(name: str, default: str = "") -> list[str]:
    value = _getenv(name, default) or ""
    normalized = value.replace("\n", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _getenv_bool(name: str, default: bool = False) -> bool:
    value = _getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    @property
    def admin_bot_token(self) -> str:
        return _getenv_required("ADMIN_BOT_TOKEN")

    @property
    def admin_ids(self) -> list[int]:
        return _getenv_int_list("ADMIN_IDS")

    @property
    def database_url(self) -> str:
        return _getenv_required("DATABASE_URL")

    @property
    def openai_api_key(self) -> str:
        return _getenv_required("OPENAI_API_KEY")

    @property
    def openai_model(self) -> str:
        return _getenv("OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini"

    @property
    def ai_topic_prompt(self) -> str:
        return (
            _getenv(
                "AI_TOPIC_PROMPT",
                (
                    "Определи основную тему Telegram-поста. Ответь строго JSON: "
                    '{"topic":"краткая тема до 5 слов","confidence":0.0,'
                    '"reason":"почему выбрана тема до 12 слов"}'
                ),
            )
            or ""
        )

    @property
    def post_trigger_words(self) -> list[str]:
        return _getenv_str_list(
            "POST_TRIGGER_WORDS",
            (
                "война,обстрел,обстріл,ракета,шахед,смерть,погиб,загинув,"
                "теракт,мобилизация,мобілізація,похорон,донат,сбор,збір"
            ),
        )

    @property
    def forbidden_topics(self) -> list[str]:
        return _getenv_str_list(
            "FORBIDDEN_TOPICS",
            (
                "политика,война,трагедии,религия,медицина,смерть,теракты,"
                "катастрофы,преступления,дети в опасных ситуациях,мобилизация,"
                "национальные конфликты,похороны,тяжёлые болезни,"
                "благотворительные сборы"
            ),
        )

    @property
    def ai_forbidden_topic_prompt(self) -> str:
        return (
            _getenv(
                "AI_FORBIDDEN_TOPIC_PROMPT",
                (
                    "Проверь, относится ли Telegram-пост к одной из запрещённых тем. "
                    "Ответь строго JSON: "
                    '{"forbidden":false,"matched_topic":null,"confidence":0.0,'
                    '"reason":"краткая причина до 12 слов","topic":"краткая тема поста"}'
                ),
            )
            or ""
        )

    @property
    def telegram_api_id(self) -> int:
        return int(_getenv_required("TELEGRAM_API_ID"))

    @property
    def telegram_api_hash(self) -> str:
        return _getenv_required("TELEGRAM_API_HASH")

    @property
    def telegram_session_name(self) -> str:
        return _getenv("TELEGRAM_SESSION_NAME", "comments_ai_bot") or "comments_ai_bot"

    @property
    def app_env(self) -> AppEnv:
        return AppEnv(_getenv("APP_ENV", AppEnv.LOCAL.value) or AppEnv.LOCAL.value)

    @property
    def log_level(self) -> str:
        return _getenv("LOG_LEVEL", "INFO") or "INFO"

    @property
    def tgstat_import_max_pages(self) -> int:
        return int(_getenv("TGSTAT_IMPORT_MAX_PAGES", "5") or "5")

    @property
    def tgstat_import_max_channels(self) -> int:
        return int(_getenv("TGSTAT_IMPORT_MAX_CHANNELS", "30000") or "30000")

    @property
    def tgstat_import_target_channels(self) -> int:
        return int(_getenv("TGSTAT_IMPORT_TARGET_CHANNELS", "10000") or "10000")

    @property
    def tgstat_import_concurrency(self) -> int:
        return int(_getenv("TGSTAT_IMPORT_CONCURRENCY", "8") or "8")

    @property
    def tgstat_validate_channels(self) -> bool:
        return _getenv_bool("TGSTAT_VALIDATE_CHANNELS", False)

    @property
    def tgstat_import_categories(self) -> list[str]:
        value = _getenv(
            "TGSTAT_IMPORT_CATEGORIES",
            (
                "news,blogs,humor,tech,business,crypto,travel,marketing,"
                "psychology,education,sport,fashion,health,apps,video,"
                "music,games,food,telegram,sales,transport,other"
            ),
        )
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    @property
    def tgstat_import_sorts(self) -> list[str]:
        value = _getenv("TGSTAT_IMPORT_SORTS", "members,reach,ci,members_t,members_y")
        return [item.strip() for item in (value or "").split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
