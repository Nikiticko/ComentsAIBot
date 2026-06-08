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

Кнопка `Посты 20к+` в админке парсит активные каналы из базы только за последние 24 часа.

Кнопка `Авто каналы TGStat` добирает базу до целевого количества каналов из публичных
рейтингов TGStat по нескольким категориям и сортировкам. HTTP-страницы TGStat грузятся
параллельно, а username добавляются в базу без Telethon-проверки, чтобы не ловить
Telegram FloodWait на массовом ResolveUsername. Лимиты можно менять переменными
`TGSTAT_IMPORT_TARGET_CHANNELS`, `TGSTAT_IMPORT_MAX_PAGES`, `TGSTAT_IMPORT_MAX_CHANNELS`,
`TGSTAT_IMPORT_CONCURRENCY`, `TGSTAT_IMPORT_CATEGORIES`, `TGSTAT_IMPORT_SORTS`.
Если нужна медленная проверка через Telegram перед добавлением, включи
`TGSTAT_VALIDATE_CHANNELS=true`.
По умолчанию цель импорта — 10 000 каналов.

## Dev-запуск

Для разработки на macOS используй `run_dev.command`. Он запускает бота с авто-рестартом при изменениях в коде.

```bash
./run_dev.command
```
