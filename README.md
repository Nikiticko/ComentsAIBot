# Comments AI Bot

MVP-система для автоматической публикации нативных комментариев под постами публичных Telegram-каналов.

## Стек

- Python 3.12
- aiogram 3
- Telethon
- SQLite
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
2. Создать `.env` по примеру `.env.example` и заполнить обязательные переменные.
3. Создать папку под SQLite:

```bash
mkdir -p data
```

4. Применить миграции:

```bash
alembic upgrade head
```

5. Запустить админ-бота:

```bash
comments-admin-bot
```

На macOS можно запускать через `run_bot.command`. Он сам создаёт `.venv`, ставит зависимости, создаёт папку `data`, проверяет `.env`, применяет миграции и запускает бота.

## Авторизация Telegram-аккаунта

Перед парсингом каналов нужно один раз авторизовать Telegram-аккаунт для Telethon:

```bash
source .venv/bin/activate
python scripts/auth_telegram.py
```

Скрипт попросит номер телефона, код Telegram и пароль 2FA, если он включён. После этого сессия сохранится в `data`.

Кнопка `Израиль каналы` добирает базу от seed-каналов через обычный Telegram-аккаунт:
читает описание и последние посты, ищет упоминания публичных `@username`, проверяет
иврит и израильские маркеры, затем добавляет подходящие каналы в базу.
Лимиты можно менять переменными `ISRAEL_DISCOVERY_TARGET_CHANNELS`,
`ISRAEL_DISCOVERY_MAX_SCANNED_CHANNELS`, `ISRAEL_DISCOVERY_POST_LIMIT`,
`ISRAEL_DISCOVERY_MIN_VIEWS`, `ISRAEL_DISCOVERY_SEARCH_LIMIT`, `ISRAEL_DISCOVERY_MAX_DEPTH`,
`ISRAEL_DISCOVERY_SEED_CHANNELS`, `ISRAEL_DISCOVERY_SEARCH_QUERIES`.
Канал добавляется только если найден свежий пост с открытыми комментариями и
просмотрами не ниже `ISRAEL_DISCOVERY_MIN_VIEWS`.

TGStat-импорт оставлен как вспомогательный источник. HTTP-страницы TGStat грузятся
параллельно, а username добавляются в базу без Telethon-проверки, чтобы не ловить
Telegram FloodWait на массовом ResolveUsername. Лимиты можно менять переменными
`TGSTAT_IMPORT_TARGET_CHANNELS`, `TGSTAT_IMPORT_MAX_PAGES`, `TGSTAT_IMPORT_MAX_CHANNELS`,
`TGSTAT_IMPORT_CONCURRENCY`, `TGSTAT_IMPORT_CATEGORIES`, `TGSTAT_IMPORT_SORTS`,
`TGSTAT_IMPORT_SOURCE_URLS`, `TGSTAT_IMPORT_KEYWORDS`.
Если `TGSTAT_IMPORT_SOURCE_URLS` задан, импорт идёт только по этим TGStat URL.
Если нужна медленная проверка через Telegram перед добавлением, включи
`TGSTAT_VALIDATE_CHANNELS=true`.
По умолчанию цель импорта — 10 000 каналов.

## Dev-запуск

Для разработки на macOS используй `run_dev.command`. Он запускает бота с авто-рестартом при изменениях в коде.

```bash
./run_dev.command
```
