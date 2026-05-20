FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir -e .

CMD ["sh", "-c", "mkdir -p data && alembic upgrade head && comments-admin-bot"]
