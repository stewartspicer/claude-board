FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 DATA_DIR=/data PATH=/app/.venv/bin:$PATH

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY board ./board
RUN useradd --uid 99 --gid 100 --no-create-home --shell /usr/sbin/nologin board 2>/dev/null || true
# Own /data so a fresh named volume (docker compose) is writable by the app user.
RUN mkdir -p /data && chown 99:100 /data
USER 99:100

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "board.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
