FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv/convoke

# curl for container healthchecks; ffmpeg for video frame sampling (media understanding)
RUN apt-get update && apt-get install -y --no-install-recommends curl ffmpeg && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN uv sync --frozen --no-dev

ENV PATH="/srv/convoke/.venv/bin:$PATH"

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
