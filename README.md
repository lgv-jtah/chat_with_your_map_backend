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

**Data services:** still **stubbed** (`cwym_agent.stubs.data_services.
StubDataServices`) - real vector-search/geodata-API integration is a
separate, later phase.

Swapping in the real data services only requires replacing
`StubDataServices` (passed into `build_pipeline(data_services=...)`) -
the components, bridge and endpoint do not need to change.

Note: the endpoint path is `/api/query` (not `/query` as in the
architecture doc's pseudocode), to match what the `chatWithYourMap`
Masterportal addon frontend already calls.

## Requirements

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)
- Network access to the Qwen3 vLLM server (see `CWYM_QWEN3_BASE_URL`
  below; defaults to the internal deployment used during development)

## Configuration

All optional, read from the environment (see `cwym_agent.settings.
Qwen3Settings`):

| Variable | Default | Purpose |
|---|---|---|
| `CWYM_QWEN3_BASE_URL` | `http://10.200.160.182:5555/v1` | vLLM server's OpenAI-compatible base URL |
| `CWYM_QWEN3_MODEL` | `Qwen/Qwen3.8-27B` | Model name to request |
| `CWYM_QWEN3_API_KEY` | `EMPTY` | API key (the server tested against requires no real auth) |
| `CWYM_QWEN3_TIMEOUT` | `60` | Request timeout (seconds) |
| `CWYM_QWEN3_MAX_TOKENS_PLAN` | `1024` | Max tokens for the planning call (covers the reasoning trace + tool calls) |
| `CWYM_QWEN3_MAX_TOKENS_GENERATE` | `512` | Max tokens for the final reply generation call |

## Setup

```bash
uv sync
```

## Run

```bash
uv run uvicorn cwym_agent.api.main:app --port 8082 --reload
```

## Try it

```bash
curl -N -X POST http://localhost:8082/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Zeige mir Fahrradstationen in Wandsbek", "include_reasoning": true, "language": "German"}'
```

This streams an SSE event sequence:
`RUN_STARTED` → `REASONING_*` → `TOOL_CALL_*` (one per sub-agent Qwen3
decides to call - zero, one, or several) → `STATE_SNAPSHOT` →
`TEXT_MESSAGE_*` → `RUN_FINISHED`.

## Tests

```bash
uv run pytest
```

Fast/deterministic by default - the pipeline/API tests
(`test_api.py`) run against `StubQwen3Generator`/`StubDataServices` via
a FastAPI dependency override, and `test_qwen3_client.py` mocks the
HTTP layer directly, so no network access is required.

To additionally run the opt-in end-to-end test against the real
Qwen3/vLLM server (requires network access to it):

```bash
CWYM_LIVE_TESTS=1 uv run pytest tests/test_qwen3_live.py
```
