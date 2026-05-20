# Comments AI Bot

MVP-система для автоматической публикации нативных комментариев под постами публичных Telegram-каналов.

## Стек

- Python 3.12
- aiogram 3
- Telethon
- PostgreSQL
- SQLAlchemy 2
- Alembic
- APScheduler
- OpenAI API

## Быстрый старт

1. Создать виртуальное окружение:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

2. Создать `.env` по примеру `.env.example`.
3. Запустить админ-бота:

```bash
comments-admin-bot
```

На macOS можно запускать через `run_bot.command`.
