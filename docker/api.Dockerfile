FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY README.md ./
RUN uv sync --frozen --no-dev
CMD ["uv", "run", "uvicorn", "recipe_normalizer.main:app", "--host", "0.0.0.0", "--port", "8000"]
