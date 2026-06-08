import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from comments_ai_bot.core.logging import setup_logging  # noqa: E402

WATCH_PATHS = (
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "alembic",
    PROJECT_ROOT / "pyproject.toml",
)
WATCH_SUFFIXES = {".py", ".toml", ".ini"}
POLL_INTERVAL_SECONDS = 1


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in WATCH_PATHS:
        if path.is_file() and path.suffix in WATCH_SUFFIXES:
            files.append(path)
            continue

        if path.is_dir():
            files.extend(
                file
                for file in path.rglob("*")
                if file.is_file()
                and file.suffix in WATCH_SUFFIXES
                and "__pycache__" not in file.parts
            )
    return files


def snapshot() -> dict[Path, float]:
    return {file: file.stat().st_mtime for file in iter_files()}


def start_bot() -> subprocess.Popen:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT, check=True)
    return subprocess.Popen([sys.executable, "-m", "comments_ai_bot.admin_bot.main"], cwd=PROJECT_ROOT)


def stop_bot(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=5)


def main() -> int:
    setup_logging()
    print("Dev watcher started. Press Ctrl+C to stop.")
    state = snapshot()
    process = start_bot()

    try:
        while True:
            time.sleep(POLL_INTERVAL_SECONDS)
            new_state = snapshot()
            if new_state != state:
                print("Changes detected. Restarting bot...")
                stop_bot(process)
                process = start_bot()
                state = new_state

            if process.poll() is not None:
                print(f"Bot process exited with code {process.returncode}. Restarting...")
                process = start_bot()
                state = snapshot()
    except KeyboardInterrupt:
        stop_bot(process)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
