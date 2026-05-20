import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

ENV_FILE = Path(".env")
REQUIRED_KEYS = (
    "ADMIN_BOT_TOKEN",
    "ADMIN_IDS",
    "DATABASE_URL",
    "OPENAI_API_KEY",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
)


def main() -> int:
    if not ENV_FILE.exists():
        print("Не найден файл .env")
        print("Создай его рядом с .env.example и заполни переменные.")
        return 1

    values = {}
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")

    missing = [key for key in REQUIRED_KEYS if not values.get(key)]

    if missing:
        print("В .env не заполнены обязательные переменные:")
        for key in missing:
            print(f"- {key}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
