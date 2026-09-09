"""FastAPI application exposing the CWYM-Agent pipeline over AG-UI/SSE.

The endpoint path and request body intentionally match what the
``chatWithYourMap`` Masterportal addon frontend already sends (see
``ChatWithYourMap.vue``), so the frontend only needs to switch from
one-shot JSON parsing to consuming an SSE stream of AG-UI events -
no request-shape changes required.
"""

import asyncio
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.pipeline import build_pipeline, run_pipeline

app = FastAPI(title="CWYM-Agent")

# The Masterportal dev server and deployed portal run on a different
# origin than this backend; allow all origins for now since this is a
# local/dev integration - tighten before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# A single pipeline instance is reused across requests; Haystack
# components are stateless here (all per-request state flows through
# pipeline.run() arguments), so this is safe to share.
_pipeline = build_pipeline()


class QueryRequest(BaseModel):
    query: str
    include_reasoning: bool = False
    language: str = "German"


@app.post("/api/query")
async def query_endpoint(request: QueryRequest):
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    bridge = AGUIEventBridge(thread_id=thread_id, run_id=run_id)

    def run_pipeline_sync():
        bridge.run_started()
        try:
            run_pipeline(_pipeline, request.query, bridge=bridge)
            bridge.run_finished()
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as RUN_ERROR
            bridge.run_error(str(exc))
        finally:
            bridge.done()

    async def event_stream():
        loop = asyncio.get_event_loop()
        task = loop.run_in_executor(None, run_pipeline_sync)

        while True:
            event = await loop.run_in_executor(None, bridge.drain)
            if event is None:
                break
            yield f"data: {event.model_dump_json(by_alias=True, exclude_none=True)}\n\n"

        await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {"status": "ok"}
