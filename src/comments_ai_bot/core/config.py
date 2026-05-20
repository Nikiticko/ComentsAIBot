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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
