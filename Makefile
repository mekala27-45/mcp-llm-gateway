.PHONY: install lint typecheck test check

install:
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	@for p in packages/*; do uv run mypy $$p/src $$p/tests || exit 1; done

test:
	uv run pytest --cov --cov-report=term-missing

check: lint typecheck test
