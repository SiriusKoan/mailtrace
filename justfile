format:
    uv run isort mailtrace/
    uv run black mailtrace/

lint:
    uv run flake8 mailtrace/
    uv run pyright mailtrace/
