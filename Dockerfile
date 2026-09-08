FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY mailtrace ./mailtrace

RUN uv sync --locked --no-dev --group tracing

CMD ["python3", "-m", "mailtrace", "tracing"]
