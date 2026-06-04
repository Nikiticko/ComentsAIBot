# Деплой на Ubuntu VPS без Docker

Рекомендуемый путь проекта: `/var/www/ComentsAIBot`.

## 1. Подготовить сервер

```bash
apt update
apt install -y git python3.12 python3.12-venv python3-pip
```

Если сервер пишет `System restart required`, лучше сначала перезагрузить:

```bash
reboot
```

## 2. Загрузить проект

Через git:

```bash
cd /var/www
git clone <repo_url> ComentsAIBot
cd /var/www/ComentsAIBot
```

Если проект уже лежит на сервере:

```bash
cd /var/www/ComentsAIBot
git pull
```

## 3. Установить зависимости

```bash
cd /var/www/ComentsAIBot
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
mkdir -p data logs
```

## 4. Создать `.env`

```bash
cp .env.example .env
nano .env
```

Минимальные переменные:

```env
ADMIN_BOT_TOKEN=...
ADMIN_IDS=...
DATABASE_URL=sqlite+aiosqlite:///data/comments_ai_bot.db
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION_NAME=comments_ai_bot
# TELEGRAM_PROXY_URL=socks5://user:password@host:port
LOG_LEVEL=INFO
```

## 5. Применить миграции

```bash
cd /var/www/ComentsAIBot
source .venv/bin/activate
alembic upgrade head
```

## 6. Авторизовать Telegram-аккаунт

```bash
cd /var/www/ComentsAIBot
source .venv/bin/activate
python scripts/auth_telegram.py
```

Сессия сохранится в `data/`. Дополнительные аккаунты можно добавлять через кнопку `Аккаунты TG`.

Если авторизация падает с ошибкой `Connection to Telegram failed`, значит VPS не может
подключиться к Telegram напрямую. Тогда добавь в `.env` прокси:

```env
TELEGRAM_PROXY_URL=socks5://user:password@host:port
```

Поддерживаются форматы:

```env
TELEGRAM_PROXY_URL=socks5://host:port
TELEGRAM_PROXY_URL=socks5://user:password@host:port
TELEGRAM_PROXY_URL=http://host:port
```

После изменения `.env` повтори:

```bash
source .venv/bin/activate
pip install -e .
python scripts/auth_telegram.py
```

## 7. Создать systemd-сервис

```bash
nano /etc/systemd/system/comments-ai-bot.service
```

Вставить:

```ini
[Unit]
Description=Comments AI Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/var/www/ComentsAIBot
Environment=PYTHONUNBUFFERED=1
ExecStart=/var/www/ComentsAIBot/.venv/bin/comments-admin-bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Запустить:

```bash
systemctl daemon-reload
systemctl enable --now comments-ai-bot
systemctl status comments-ai-bot
```

## 8. Логи и управление

Смотреть логи:

```bash
journalctl -u comments-ai-bot -f
```

Перезапустить:

```bash
systemctl restart comments-ai-bot
```

Остановить:

```bash
systemctl stop comments-ai-bot
```

## 9. Обновление

```bash
cd /var/www/ComentsAIBot
git pull
source .venv/bin/activate
pip install -e .
alembic upgrade head
systemctl restart comments-ai-bot
journalctl -u comments-ai-bot -f
```

## Важно

- Не запускай два экземпляра с одним `ADMIN_BOT_TOKEN`.
- Не удаляй `data/`, там база SQLite и Telegram-сессии.
- Не коммить `.env`.
