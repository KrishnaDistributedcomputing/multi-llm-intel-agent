FROM ghcr.io/astral-sh/uv:0.9.5 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && groupadd --gid 10001 agent && useradd --uid 10001 --gid agent agent \
    && mkdir /app/data && chown 10001:10001 /app/data
COPY agent ./agent
COPY models.yaml ./
USER 10001:10001
EXPOSE 8000
CMD [".venv/bin/uvicorn", "agent.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "16", "--timeout-keep-alive", "5", "--no-access-log"]
