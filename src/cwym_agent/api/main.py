"""FastAPI application exposing the CWYM-Agent pipeline over AG-UI/SSE.

The endpoint path and request body intentionally match what the
``chatWithYourMap`` Masterportal addon frontend already sends (see
``ChatWithYourMap.vue``), so the frontend only needs to switch from
one-shot JSON parsing to consuming an SSE stream of AG-UI events -
no request-shape changes required.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from haystack import Pipeline
from pydantic import BaseModel

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.pipeline import build_pipeline, run_pipeline


@lru_cache(maxsize=1)
def get_pipeline() -> Pipeline:
    """Build the (real-backend) pipeline, once.

    A FastAPI dependency rather than a bare module-level singleton so
    tests can override it (via ``app.dependency_overrides``) with a
    stub-backed pipeline, without needing network access to the real
    Qwen3/vLLM server or the real geodata services.
    """

    return build_pipeline()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly build (and warm up) the pipeline at startup rather than
    # lazily on the first request: RealDataServices.warm_up() fetches
    # and embeds Hamburg's ~6,500 map layers, which takes a few minutes
    # - better to pay that cost (and fail fast if the layer catalog or
    # embedding model is unreachable) before accepting traffic than to
    # block a user's first query.
    get_pipeline()
    yield


app = FastAPI(title="CWYM-Agent", lifespan=lifespan)

# The Masterportal dev server and deployed portal run on a different
# origin than this backend; allow all origins for now since this is a
# local/dev integration - tighten before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    query: str
    include_reasoning: bool = False
    language: str = "German"


@app.post("/api/query")
async def query_endpoint(request: QueryRequest, pipeline: Pipeline = Depends(get_pipeline)):
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    bridge = AGUIEventBridge(thread_id=thread_id, run_id=run_id)

    def run_pipeline_sync():
        bridge.run_started()
        try:
            run_pipeline(pipeline, request.query, bridge=bridge)
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
