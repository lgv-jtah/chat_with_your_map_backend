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


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


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

    # How long PortalCatalogResolver trusts a previously-fetched
    # config.json before re-fetching (cwym-agent-architecture.md Section
    # 8) - a plain TTL rather than the doc's ETag/conditional-GET refresh,
    # since portal configs change rarely and a single real deployment
    # doesn't need that extra complexity yet.
    portal_catalog_cache_ttl: float = field(
        default_factory=lambda: _env_float("CWYM_PORTAL_CATALOG_CACHE_TTL", 600.0)
    )

    # PortalCatalogResolver fetches a client-supplied URL server-side -
    # restrict it (SSRF hardening). Empty by default: only the baseline
    # scheme/private-IP checks apply; set this to require an explicit
    # hostname allowlist on top of them.
    portal_catalog_allowed_hosts: frozenset[str] = field(
        default_factory=lambda: frozenset(
            h.strip()
            for h in os.environ.get("CWYM_PORTAL_CATALOG_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        )
    )
    portal_catalog_max_response_bytes: int = field(
        default_factory=lambda: _env_int("CWYM_PORTAL_CATALOG_MAX_RESPONSE_BYTES", 5_000_000)
    )
    # Defaults to True: this project's only real deployment today runs
    # frontend and backend on the same host, so the frontend's own
    # resolved config.json URL legitimately is loopback - same "tighten
    # before real deployment" posture as the wide-open CORS setting.
    portal_catalog_allow_loopback: bool = field(
        default_factory=lambda: _env_bool("CWYM_PORTAL_CATALOG_ALLOW_LOOPBACK", True)
    )

    # FilterAgent (cwym-agent-architecture.md Section 9): CQL2 filtering
    # against a layer's own OGC API Features endpoint.
    filter_queryables_cache_ttl: float = field(
        default_factory=lambda: _env_float("CWYM_FILTER_QUERYABLES_CACHE_TTL", 3600.0)
    )
    filter_result_limit: int = field(
        default_factory=lambda: _env_int("CWYM_FILTER_RESULT_LIMIT", 500)
    )
    # Same rationale as layer_llm_max_tokens - condition extraction is a
    # small-output task but Qwen3's reasoning trace precedes it.
    filter_llm_max_tokens: int = field(
        default_factory=lambda: _env_int("CWYM_FILTER_LLM_MAX_TOKENS", 2048)
    )
