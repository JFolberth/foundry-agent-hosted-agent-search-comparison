FROM ghcr.io/astral-sh/uv:0.12.0 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src UV_CACHE_DIR=/app/.uv-cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && uv cache clean --quiet
COPY config/ config/
COPY src/comparison/ src/comparison/
COPY src/web/ src/web/
RUN useradd --uid 10001 --create-home app && chown -R app:app /app
USER app
EXPOSE 8080
CMD [".venv/bin/uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
