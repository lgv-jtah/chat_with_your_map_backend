"""Runtime configuration for the real Qwen3 backend.

Plain environment-variable-driven settings (no pydantic-settings
dependency needed for this handful of values) so the backend can be
pointed at a different vLLM deployment (or model) without code changes.
"""

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Qwen3Settings:
    """Connection/generation settings for the Qwen3 vLLM server."""

    base_url: str = field(
        default_factory=lambda: os.environ.get(
            "CWYM_QWEN3_BASE_URL", "http://10.200.160.182:5555/v1"
        )
    )
    model: str = field(
        default_factory=lambda: os.environ.get("CWYM_QWEN3_MODEL", "Qwen/Qwen3.8-27B")
    )
    # The vLLM server tested against requires no real authentication; the
    # OpenAI SDK still requires a non-empty string to be passed.
    api_key: str = field(
        default_factory=lambda: os.environ.get("CWYM_QWEN3_API_KEY", "EMPTY")
    )
    timeout: float = field(default_factory=lambda: _env_float("CWYM_QWEN3_TIMEOUT", 60.0))
    # 1024 was enough for a bare-query planning prompt, but the
    # conversation-context block + planning rules (Section 10.5) added
    # since give Qwen3 noticeably more to reason through before it
    # starts emitting tool calls - found live: a context-enriched
    # prompt's reasoning trace was still mid-thought
    # ("...action_detector: maybe... But if filter_agent already
    # called, maybe action_detector") when the budget ran out, leaving
    # zero or malformed tool calls (a "Qwen3 returned malformed JSON
    # arguments" warning, or an empty plan) for a turn that should have
    # produced a real plan. Matches the budget already used for
    # similarly reasoning-heavy classification-style calls elsewhere in
    # this codebase (layer_llm_max_tokens/filter_llm_max_tokens, both
    # 2048).
    max_tokens_plan: int = field(
        default_factory=lambda: _env_int("CWYM_QWEN3_MAX_TOKENS_PLAN", 2048)
    )
    max_tokens_generate: int = field(
        default_factory=lambda: _env_int("CWYM_QWEN3_MAX_TOKENS_GENERATE", 512)
    )
