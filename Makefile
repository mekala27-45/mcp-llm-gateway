.PHONY: install lint typecheck test check bench bench-quick

install:
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	@for p in packages/*; do uv run mypy $$p/src $$p/tests || exit 1; done
	uv run mypy bench

test:
	uv run pytest --cov --cov-report=term-missing

check: lint typecheck test

bench:
	uv run python bench/stream_bench.py

bench-quick:
	uv run python bench/stream_bench.py --quick --out results/bench-quick.json
