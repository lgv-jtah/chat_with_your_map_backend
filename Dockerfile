# syntax=docker/dockerfile:1

# ---- build stage: resolve deps from uv.lock into /app/.venv ----------------
FROM python:3.12-slim AS builder

# Same uv version as the local toolchain (pyproject pins uv_build <0.10).
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install third-party deps first (cached layer - only invalidated when the
# lockfile changes, not on every source edit). torch comes from the CPU-only
# index configured in pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Then the project itself.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime stage --------------------------------------------------------
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 app

# Pre-create the Hugging Face cache dir so the named volume mounted there
# inherits app-user ownership (the embedding/reranker models are downloaded
# on first start and should survive container recreation).
ENV HF_HOME=/home/app/.cache/huggingface \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
RUN mkdir -p "$HF_HOME" && chown -R app:app /home/app/.cache

COPY --from=builder --chown=app:app /app/.venv /app/.venv

USER app
WORKDIR /app

EXPOSE 8082

CMD ["uvicorn", "cwym_agent.api.main:app", "--host", "0.0.0.0", "--port", "8082"]
