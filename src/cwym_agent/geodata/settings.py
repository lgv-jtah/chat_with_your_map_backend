"""Runtime configuration for the real geodata services.

Plain environment-variable-driven settings, mirroring
``cwym_agent.settings.Qwen3Settings``.
"""

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class GeodataSettings:
    """Connection/search settings for Hamburg's public geodata APIs."""

    layer_search_url: str = field(
        default_factory=lambda: os.environ.get(
            "CWYM_LAYER_SEARCH_URL", "https://geodienste.hamburg.de/layers/_search"
        )
    )
    layer_embed_model: str = field(
        default_factory=lambda: os.environ.get(
            "CWYM_LAYER_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        )
    )
    layer_ranker_model: str = field(
        default_factory=lambda: os.environ.get(
            "CWYM_LAYER_RANKER_MODEL", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
        )
    )
    layer_retriever_top_k: int = field(
        default_factory=lambda: _env_int("CWYM_LAYER_RETRIEVER_TOP_K", 20)
    )
    layer_result_top_k: int = field(
        default_factory=lambda: _env_int("CWYM_LAYER_RESULT_TOP_K", 15)
    )
    layer_fetch_size: int = field(
        default_factory=lambda: _env_int("CWYM_LAYER_FETCH_SIZE", 10000)
    )
    # The layer catalog endpoint is intermittently flaky (observed
    # ~40% of requests timing out at 15s while others complete in
    # under a second) - retry a handful of times with backoff rather
    # than crashing app startup on a single transient timeout.
    layer_fetch_retries: int = field(
        default_factory=lambda: _env_int("CWYM_LAYER_FETCH_RETRIES", 5)
    )
    layer_fetch_retry_backoff: float = field(
        default_factory=lambda: _env_float("CWYM_LAYER_FETCH_RETRY_BACKOFF", 2.0)
    )
    # Budget for the LLM query-expansion/disambiguation calls in
    # RealDataServices - these need generous headroom since Qwen3's
    # reasoning trace precedes even a short final answer, and can be
    # substantial for a "pick the best of N candidates"-style prompt.
    layer_llm_max_tokens: int = field(
        default_factory=lambda: _env_int("CWYM_LAYER_LLM_MAX_TOKENS", 2048)
    )

    address_search_url: str = field(
        default_factory=lambda: os.environ.get(
            "CWYM_ADDRESS_SEARCH_URL", "https://api.hamburg.de/addr_search"
        )
    )
    address_result_size: int = field(
        default_factory=lambda: _env_int("CWYM_ADDRESS_RESULT_SIZE", 20)
    )

    timeout: float = field(default_factory=lambda: _env_float("CWYM_GEODATA_TIMEOUT", 15.0))
