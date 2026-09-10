.DEFAULT_GOAL := help

.PHONY: help install run test test-live clean

help:
	@echo "Available targets:"
	@echo "  install    Install dependencies via uv sync"
	@echo "  run        Start the dev server (uvicorn --reload) on port 8082"
	@echo "  test       Run the fast/offline test suite"
	@echo "  test-live  Run opt-in live tests against real Qwen3/geodata APIs"
	@echo "  clean      Remove Python/pytest cache artifacts"

install:
	uv sync

run:
	uv run uvicorn cwym_agent.api.main:app --port 8082 --reload

test:
	uv run pytest

test-live:
	CWYM_LIVE_TESTS=1 uv run pytest tests/test_qwen3_live.py tests/test_geodata_live.py

clean:
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
	rm -rf .pytest_cache
