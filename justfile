format:
    uv run ruff format mailtrace/ tests/
    uv run ruff check --fix mailtrace/ tests/

lint:
    uv run ruff check mailtrace/ tests/
    uv run pyright mailtrace/

test:
    uv run pytest tests/ -v

int-test:
    uv run pytest -m e2e -v
