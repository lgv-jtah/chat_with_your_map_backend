# CWYM-Agent

AG-UI streaming backend for **Chat With Your Map** (CWYM) — a chatbot
layer on top of Hamburg's Masterportal (Vue 3 geoinformation system) that
lets users control the map and query geodata using natural German
language.

See `../cwym-agent-architecture.md` for the full architecture writeup
(LLM backend, AG-UI event mapping, design rationale).

## Status

This implements the architecture doc's pipeline shape end-to-end.

- `MasterAgentComponent` → `AgentExecutorComponent` →
  `ResultAggregatorComponent` → `TextResponseComponent`, a Haystack
  pipeline (`cwym_agent.pipeline`)
- `AGUIEventBridge` (`cwym_agent.bridge`) pushes AG-UI events from the
  pipeline's worker thread into a queue drained by the FastAPI endpoint
- `POST /api/query` (`cwym_agent.api.main`) streams those events as
  Server-Sent Events

**LLM backend:** `Qwen3Client` (`cwym_agent.llm.qwen3_client`) talks to
the real Qwen3 model over an OpenAI-compatible vLLM server. Qwen3's
`<think>` reasoning trace comes back as a separate `reasoning` field
(a vLLM/Qwen3 extension, not part of the official OpenAI schema) -
that's why this is a small custom client built on the `openai` SDK
rather than Haystack's built-in `OpenAIChatGenerator`, which only reads
`.content`/`.tool_calls` and would silently drop the reasoning trace.
Planning (`plan()`) uses native OpenAI-style tool calling so Qwen3
itself decides which of `location_extractor`/`layer_resolver`/
`action_detector` (defined once in `cwym_agent.sub_agents`) are
relevant to a given query - zero, one, or several may be called.

**Data services:** real, backed by Hamburg's public APIs
(`cwym_agent.geodata`):

- `AddressResolver` resolves district/street names to coordinates via
  `https://api.hamburg.de/addr_search` (public, unauthenticated). Prefers
  district/city/county-level hits over individual streets, since a query
  like "Wandsbek" almost always means the district, not one of the dozens
  of "Wandsbeker ..." streets that otherwise dominate by raw
  Elasticsearch score.
- `LayerSearchService` does hybrid BM25 + embedding retrieval + cross-
  encoder reranking (`InMemoryDocumentStore`, `sentence-transformers/
  all-MiniLM-L6-v2`, `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`) over
  Hamburg's ~6,500 map layers, fetched from `https://geodienste.hamburg.
  de/layers/_search` (public, unauthenticated).
- **Important finding:** pure text/embedding similarity cannot bridge
  colloquial terms to Hamburg's official layer names when they share no
  lexical overlap - e.g. "Fahrradstationen" scores far closer to
  "Fahrradluftstationen" (bike air-pump stations) than to the actually-
  correct "StadtRAD-Stationen Hamburg" (Hamburg's bike-sharing brand
  name), verified empirically against the real catalog. So
  `RealDataServices` adds two LLM-assisted steps around retrieval: Qwen3
  first proposes likely official term(s) for the query (e.g.
  "Fahrradstationen" -> "StadtRAD"), which are searched too and merged
  with the original query's results, then Qwen3 picks the actual best
  match from that shortlist. Both steps need a generous `max_tokens`
  budget (`CWYM_LAYER_LLM_MAX_TOKENS`) since Qwen3's reasoning trace
  precedes even a short classification-style answer.
- `detect_action` stays simple local keyword matching - no external
  service needed for that.

The layer search index is built once, eagerly, at application startup
(fetching + embedding ~6,500 documents takes a few minutes on CPU) - see
`RealDataServices.warm_up()`, called from the FastAPI `lifespan` handler
in `api/main.py`.

Swapping either piece out only requires passing different objects into
`build_pipeline(llm=..., data_services=...)` - the components, bridge
and endpoint do not need to change.

Note: the endpoint path is `/api/query` (not `/query` as in the
architecture doc's pseudocode), to match what the `chatWithYourMap`
Masterportal addon frontend already calls.

## Requirements

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)
- Network access to the Qwen3 vLLM server (see `CWYM_QWEN3_BASE_URL`
  below; defaults to the internal deployment used during development)
  and to Hamburg's public geodata APIs (`api.hamburg.de`,
  `geodienste.hamburg.de`)

Note: `torch` (a `sentence-transformers` dependency, used for layer
embedding) is pinned to CPU-only wheels via `[tool.uv.sources]` in
`pyproject.toml` - the default GPU/CUDA wheels are several GB larger for
no benefit here, since embedding a small model for ~6,500 short
documents doesn't need a GPU.

## Configuration

All optional, read from the environment (see `cwym_agent.settings.
Qwen3Settings` and `cwym_agent.geodata.settings.GeodataSettings`):

| Variable | Default | Purpose |
|---|---|---|
| `CWYM_QWEN3_BASE_URL` | `http://10.200.160.182:5555/v1` | vLLM server's OpenAI-compatible base URL |
| `CWYM_QWEN3_MODEL` | `Qwen/Qwen3.8-27B` | Model name to request |
| `CWYM_QWEN3_API_KEY` | `EMPTY` | API key (the server tested against requires no real auth) |
| `CWYM_QWEN3_TIMEOUT` | `60` | Request timeout (seconds) |
| `CWYM_QWEN3_MAX_TOKENS_PLAN` | `2048` | Max tokens for the planning call (covers the reasoning trace + tool calls) - the conversation-context block + planning rules give Qwen3 more to reason through than a bare query, so this needs more headroom than it used to |
| `CWYM_QWEN3_MAX_TOKENS_GENERATE` | `512` | Max tokens for the final reply generation call |
| `CWYM_LAYER_SEARCH_URL` | `https://geodienste.hamburg.de/layers/_search` | Hamburg's public layer catalog Elasticsearch endpoint |
| `CWYM_LAYER_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model for layer search |
| `CWYM_LAYER_RANKER_MODEL` | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | Cross-encoder reranker model |
| `CWYM_LAYER_RETRIEVER_TOP_K` | `20` | Candidates pulled from each of BM25/embedding retrieval |
| `CWYM_LAYER_RESULT_TOP_K` | `15` | Final candidate shortlist size passed to LLM disambiguation |
| `CWYM_LAYER_FETCH_SIZE` | `10000` | Max layers fetched when building the index (covers the full ~6,500-layer catalog) |
| `CWYM_LAYER_FETCH_RETRIES` | `5` | Retry attempts for the layer catalog fetch (this endpoint is intermittently flaky - some requests time out while others succeed in under a second) |
| `CWYM_LAYER_FETCH_RETRY_BACKOFF` | `2` | Seconds to wait between layer catalog fetch retries |
| `CWYM_LAYER_LLM_MAX_TOKENS` | `2048` | Token budget for the layer expansion/disambiguation LLM calls |
| `CWYM_ADDRESS_SEARCH_URL` | `https://api.hamburg.de/addr_search` | Hamburg's public address/district search endpoint |
| `CWYM_ADDRESS_RESULT_SIZE` | `20` | Candidates fetched per address query (needs enough headroom to find district/county-level hits alongside street matches) |
| `CWYM_GEODATA_TIMEOUT` | `15` | Request timeout (seconds) for both geodata endpoints |
| `CWYM_PORTAL_CATALOG_CACHE_TTL` | `600` | Seconds `PortalCatalogResolver` trusts a fetched `config.json` before re-fetching |
| `CWYM_PORTAL_CATALOG_ALLOWED_HOSTS` | *(empty)* | Comma-separated hostname allowlist for `config_json_url`/`services_json_url` (SSRF hardening), checked independently for each - empty means only the baseline scheme/private-IP checks apply |
| `CWYM_PORTAL_CATALOG_MAX_RESPONSE_BYTES` | `5000000` | Max response size accepted when fetching either URL |
| `CWYM_PORTAL_CATALOG_ALLOW_LOOPBACK` | `true` | Whether `localhost`/loopback is an acceptable host for either URL - defaults on since frontend and backend currently run on the same host; set to `false` once they're on separate hosts |
| `CWYM_FILTER_QUERYABLES_CACHE_TTL` | `3600` | Seconds `FilterAgent` trusts a fetched OGC API Features `/queryables` schema before re-fetching |
| `CWYM_FILTER_RESULT_LIMIT` | `500` | Max features fetched per `FilterAgent` query |
| `CWYM_FILTER_LLM_MAX_TOKENS` | `2048` | Token budget for `FilterAgent`'s condition-extraction LLM call |

## Setup

```bash
uv sync
```

## Run

```bash
uv run uvicorn cwym_agent.api.main:app --port 8082 --reload
```

(or `make run`, see below)

Note: startup takes a few minutes on first run - it eagerly fetches and
embeds Hamburg's ~6,500 map layers (and downloads the embedding/reranker
models on the very first run) before accepting traffic. Subsequent
restarts re-fetch the catalog but reuse the already-downloaded models.
Startup progress (catalog fetch, embedding, index ready) is logged at
INFO level, so it's clear the process is working rather than stuck.
The layer catalog endpoint itself is intermittently flaky (some
requests time out while others succeed in under a second); the fetch
retries a few times with backoff (`CWYM_LAYER_FETCH_RETRIES`/
`CWYM_LAYER_FETCH_RETRY_BACKOFF`) before giving up.

## Makefile

```bash
make install    # uv sync
make run        # uv run uvicorn ... --port 8082 --reload
make test       # uv run pytest
make test-live  # opt-in live tests against real Qwen3/geodata APIs
make clean      # remove __pycache__/.pytest_cache
```

## Try it

```bash
curl -N -X POST http://localhost:8082/api/query \
  -H "Content-Type: application/json" \
  -d '{
    "threadId": "demo-session", "runId": "r1",
    "messages": [{"id": "m1", "role": "user", "content": "Zeige mir Fahrradstationen in Wandsbek"}],
    "state": null, "tools": [], "context": [], "forwardedProps": {}
  }'
```

Request body is a standard AG-UI `RunAgentInput` (`threadId`/`runId`/
`messages`/`state`/`tools`/`context`/`forwardedProps`) -
`forwardedProps.portal_context` carries the requesting portal's
`config_json_url`/`services_json_url` (Section 8 scoping); `state`
carries the live map state for conversation-memory follow-ups (Section
10) - see `cmym-agent-architecture.md` for the full shape.

This streams an SSE event sequence:
`RUN_STARTED` → `REASONING_*` → `TOOL_CALL_*` (one per sub-agent Qwen3
decides to call, interleaved with one `TOOL_CALL_*` cycle per
`MapCommand` as `command_builder` builds it) → `TEXT_MESSAGE_*` →
`RUN_FINISHED`.

## Tests

```bash
uv run pytest
```

Fast/deterministic by default - the pipeline/API tests
(`test_api.py`) run against `StubQwen3Generator`/`StubDataServices` via
a FastAPI dependency override, and `test_qwen3_client.py`/
`test_address_resolver.py`/`test_layer_search.py`/
`test_real_data_services.py` mock the HTTP layer directly, so no
network access or model downloads are required.

To additionally run the opt-in end-to-end tests against the real
Qwen3/vLLM server and Hamburg's real geodata APIs (requires network
access, and takes a few minutes for `test_geodata_live.py` since it
builds the full layer index):

```bash
CWYM_LIVE_TESTS=1 uv run pytest tests/test_qwen3_live.py tests/test_geodata_live.py
```
