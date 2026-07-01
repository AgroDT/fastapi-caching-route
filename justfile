default:
    @just --list

test args='':
    uv run --no-sync pytest {{args}}

lint:
    uv run --no-sync ruff check

format:
    uv run --no-sync ruff format

formatcheck:
    uv run --no-sync ruff format --check

typecheck:
    uv run --no-sync ty check

check: lint formatcheck typecheck test
