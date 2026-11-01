.PHONY: install lint typecheck test check bench bench-quick demo task1 task2 task3 task4

LLM_TESTS = packages/llm_gateway/tests
TASK3_TESTS = $(LLM_TESTS)/test_redact.py $(LLM_TESTS)/test_redact_properties.py \
	$(LLM_TESTS)/test_redos.py $(LLM_TESTS)/test_rewrite.py $(LLM_TESTS)/test_redaction.py \
	$(LLM_TESTS)/test_proxy.py packages/gateway_common packages/mock_providers
TASK4_TESTS = $(LLM_TESTS)/test_limiter.py $(LLM_TESTS)/test_db.py $(LLM_TESTS)/test_tenants.py \
	$(LLM_TESTS)/test_tenancy.py $(LLM_TESTS)/test_failover.py $(LLM_TESTS)/test_races.py \
	$(LLM_TESTS)/test_window.py

install:
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	@for p in packages/*; do uv run mypy $$p/src $$p/tests || exit 1; done
	uv run mypy bench demo

test:
	uv run pytest --cov --cov-report=term-missing

check: lint typecheck test

bench:
	uv run python bench/stream_bench.py

bench-quick:
	uv run python bench/stream_bench.py --quick --out results/bench-quick.json

demo:
	uv run python demo/run.py

task1:
	uv run customer-server --self-test
	uv run pytest -q packages/customer_server

task2:
	uv run python demo/run.py task2
	uv run pytest -q packages/mcp_gateway packages/mock_downstream

task3:
	uv run python demo/run.py task3
	uv run pytest -q $(TASK3_TESTS)

task4:
	uv run python demo/run.py task4
	uv run pytest -q $(TASK4_TESTS)
