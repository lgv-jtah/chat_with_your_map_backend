# CWYM-Agent

AG-UI streaming backend for **Chat With Your Map** (CWYM) — a chatbot
layer on top of Hamburg's Masterportal (Vue 3 geoinformation system) that
lets users control the map and query geodata using natural German
language.

See `../cwym-agent-architecture.md` for the full architecture writeup
(LLM backend, AG-UI event mapping, design rationale).

## Status

This implements the architecture doc's pipeline shape end-to-end, with
the LLM (Qwen3) and geodata services **stubbed** (`cwym_agent.stubs`):

- `MasterAgentComponent` → `AgentExecutorComponent` →
  `ResultAggregatorComponent` → `TextResponseComponent`, a Haystack
  pipeline (`cwym_agent.pipeline`)
- `AGUIEventBridge` (`cwym_agent.bridge`) pushes AG-UI events from the
  pipeline's worker thread into a queue drained by the FastAPI endpoint
- `POST /api/query` (`cwym_agent.api.main`) streams those events as
  Server-Sent Events

Swapping in the real Qwen3 generator and vector-search/geodata services
only requires replacing `StubQwen3Generator` and `StubDataServices` -
the components, bridge and endpoint do not need to change.

Note: the endpoint path is `/api/query` (not `/query` as in the
architecture doc's pseudocode), to match what the `chatWithYourMap`
Masterportal addon frontend already calls.

## Requirements

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)

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
`RUN_STARTED` → `REASONING_*` → `TOOL_CALL_*` (x3, one per sub-agent) →
`STATE_SNAPSHOT` → `TEXT_MESSAGE_*` → `RUN_FINISHED`.

## Tests

```bash
uv run pytest
```
