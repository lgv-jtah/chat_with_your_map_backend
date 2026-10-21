.DEFAULT_GOAL := help

.PHONY: help install run run-offvpn test test-live clean \
	docker-build docker-up docker-up-offvpn docker-down docker-logs

help:
	@echo "Available targets:"
	@echo "  install    Install dependencies via uv sync"
	@echo "  run        Start the dev server (uvicorn --reload) on port 8082"
	@echo "  run-offvpn Like run, but use Qwen3 on IONOS (no VPN needed)"
	@echo "  test       Run the fast/offline test suite"
	@echo "  test-live  Run opt-in live tests against real Qwen3/geodata APIs"
	@echo "  clean      Remove Python/pytest cache artifacts"
	@echo ""
	@echo "Docker targets:"
	@echo "  docker-build       Build the cwym-agent image"
	@echo "  docker-up          Build + start the container (detached) on port 8082"
	@echo "  docker-up-offvpn   Like docker-up, but use Qwen3 on IONOS (no VPN needed)"
	@echo "  docker-down        Stop and remove the container"
	@echo "  docker-logs        Follow the container logs (startup warm-up takes minutes)"

install:
	uv sync

run:
	uv run uvicorn cwym_agent.api.main:app --port 8082 --reload

# Off VPN the internal vLLM server is unreachable; use the same model on
# IONOS instead, taking the API key from the local LiteLLM container.
run-offvpn:
	CWYM_QWEN3_BASE_URL=https://openai.inference.de-txl.ionos.com/v1 \
	CWYM_QWEN3_API_KEY=$$(docker exec litellm-litellm-1 sh -c 'printf %s "$$IONOS_API_KEY"') \
	CWYM_QWEN3_TIMEOUT=120 \
	uv run uvicorn cwym_agent.api.main:app --port 8082 --reload

test:
	uv run pytest

test-live:
	CWYM_LIVE_TESTS=1 uv run pytest tests/test_qwen3_live.py tests/test_geodata_live.py

clean:
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
	rm -rf .pytest_cache

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

# Same IONOS override as run-offvpn, passed through to the container via
# the environment passthrough in compose.yaml.
docker-up-offvpn:
	CWYM_QWEN3_BASE_URL=https://openai.inference.de-txl.ionos.com/v1 \
	CWYM_QWEN3_API_KEY=$$(docker exec litellm-litellm-1 sh -c 'printf %s "$$IONOS_API_KEY"') \
	CWYM_QWEN3_TIMEOUT=120 \
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f
