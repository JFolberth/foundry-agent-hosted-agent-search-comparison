FROM ghcr.io/astral-sh/uv:0.12.0 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src UV_CACHE_DIR=/app/.uv-cache AGENTSERVER_STATE_ROOT=/app/.runtime
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && uv cache clean --quiet
COPY config/ config/
COPY src/comparison/ src/comparison/
COPY src/hosted_agent/ src/hosted_agent/
RUN useradd --uid 10001 --create-home app && chown -R app:app /app
USER app
EXPOSE 8088
CMD [".venv/bin/python", "-m", "hosted_agent.app"]
