#!/bin/zsh
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3.12 -m venv .venv || exit 1
fi

source .venv/bin/activate
mkdir -p data
python -m pip install -e . || exit 1
python scripts/check_env.py || exit 1
alembic upgrade head || exit 1
comments-admin-bot
