format:
    uv run isort mailtrace/ tests/
    uv run black mailtrace/ tests/

lint:
    uv run flake8 mailtrace/ tests/
    uv run pyright mailtrace/ tests/

test:
    uv run pytest tests/ -v

int-test:
    uv run pytest -m e2e -v
