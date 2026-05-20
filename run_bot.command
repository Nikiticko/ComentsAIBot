#!/bin/zsh
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3.12 -m venv .venv || exit 1
fi

source .venv/bin/activate
python -m pip install -e . || exit 1
comments-admin-bot
