import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from comments_ai_bot.core.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
NOISY_LOGGERS = (
    "aiogram.event",
    "httpx",
    "httpcore",
    "telethon",
    "telethon.network",
    "telethon.network.mtprotosender",
)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"
ISRAEL_DISCOVERY_LOG_FILE = LOG_DIR / "israel_discovery.log"
ISRAEL_DISCOVERY_LOGGER = "comments_ai_bot.discovery"


def _has_rotating_file_handler(logger: logging.Logger, log_file: Path) -> bool:
    return any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename) == log_file.resolve()
        for handler in logger.handlers
    )


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)

    root_logger = logging.getLogger()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    file_level = min(level, logging.INFO)
    root_logger.setLevel(level)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    has_console_handler = any(type(handler) is logging.StreamHandler for handler in root_logger.handlers)
    has_file_handler = _has_rotating_file_handler(root_logger, LOG_FILE)

    if not has_console_handler:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        root_logger.addHandler(console_handler)

    if not has_file_handler:
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(file_level)
        root_logger.addHandler(file_handler)

    discovery_logger = logging.getLogger(ISRAEL_DISCOVERY_LOGGER)
    discovery_logger.setLevel(logging.INFO)
    if not _has_rotating_file_handler(discovery_logger, ISRAEL_DISCOVERY_LOG_FILE):
        discovery_file_handler = RotatingFileHandler(
            ISRAEL_DISCOVERY_LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        discovery_file_handler.setFormatter(formatter)
        discovery_file_handler.setLevel(logging.INFO)
        discovery_logger.addHandler(discovery_file_handler)

    for handler in root_logger.handlers:
        if (
            isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == LOG_FILE.resolve()
        ):
            handler.setLevel(file_level)
            handler.setFormatter(formatter)

    for logger_name in NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)

    logging.captureWarnings(True)
