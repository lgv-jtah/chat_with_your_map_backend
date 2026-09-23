"""FastAPI application exposing the CWYM-Agent pipeline over AG-UI/SSE.

The endpoint path and request body intentionally match what the
``chatWithYourMap`` Masterportal addon frontend already sends (see
``ChatWithYourMap.vue``), so the frontend only needs to switch from
one-shot JSON parsing to consuming an SSE stream of AG-UI events -
no request-shape changes required.
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from haystack import Pipeline
from pydantic import BaseModel

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.geodata.portal_catalog_resolver import PortalCatalogResolver
from cwym_agent.pipeline import build_pipeline, run_pipeline

# Configure a basic root handler so `cwym_agent.*` module loggers (used
# for startup-progress messages - see the lifespan handler below, and
# ``cwym_agent.geodata.layer_search``) are actually visible. Without
# this, Python's root logger defaults to WARNING with no handler, so
# `logger.info(...)` calls are silently dropped even though uvicorn's
# *own* "INFO:     ..." lines (from its separately-configured loggers)
# still show up - misleadingly making the app look silent/stuck during
# the several-minutes-long startup warm-up.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_pipeline() -> Pipeline:
    """Build the (real-backend) pipeline, once.

    A FastAPI dependency rather than a bare module-level singleton so
    tests can override it (via ``app.dependency_overrides``) with a
    stub-backed pipeline, without needing network access to the real
    Qwen3/vLLM server or the real geodata services.
    """

    return build_pipeline()


@lru_cache(maxsize=1)
def get_portal_catalog_resolver() -> PortalCatalogResolver:
    """A FastAPI dependency (same rationale as ``get_pipeline``): tests
    override it with a fake resolver to assert on what URL it's called
    with, without needing network access."""

    return PortalCatalogResolver()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly build (and warm up) the pipeline at startup rather than
    # lazily on the first request: RealDataServices.warm_up() fetches
    # and embeds Hamburg's ~6,500 map layers, which takes a few minutes
    # - better to pay that cost (and fail fast if the layer catalog or
    # embedding model is unreachable) before accepting traffic than to
    # block a user's first query.
    logger.info(
        "Warming up pipeline (fetching + embedding Hamburg's ~6,500 map "
        "layers) - this takes a few minutes, especially on the first run "
        "which also downloads the embedding/reranker models. Not stuck, "
        "just slow; see progress below."
    )
    start = time.monotonic()
    get_pipeline()
    logger.info("Pipeline warm-up finished in %.1fs", time.monotonic() - start)
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


class PortalContext(BaseModel):
    """Matches what the frontend already resolved to boot itself
    (cwym-agent-architecture.md Section 8) - the backend never re-derives
    portal URLs on its own. ``services_json_url`` is accepted for schema
    parity with the doc but currently unused; see
    ``PortalCatalogResolver``'s module docstring for why."""

    config_json_url: str
    services_json_url: str | None = None


class QueryRequest(BaseModel):
    query: str
    include_reasoning: bool = False
    language: str = "German"
    portal_context: PortalContext | None = None


@app.post("/api/query")
async def query_endpoint(
    request: QueryRequest,
    pipeline: Pipeline = Depends(get_pipeline),
    portal_catalog_resolver: PortalCatalogResolver = Depends(get_portal_catalog_resolver),
):
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    bridge = AGUIEventBridge(thread_id=thread_id, run_id=run_id)

    config_json_url = request.portal_context.config_json_url if request.portal_context else None
    allowed_layer_ids = await asyncio.to_thread(
        portal_catalog_resolver.get_allowed_layer_ids, config_json_url
    )
    allowed_layer_ids = list(allowed_layer_ids) if allowed_layer_ids is not None else None

    def run_pipeline_sync():
        bridge.run_started()
        try:
            run_pipeline(pipeline, request.query, bridge=bridge, allowed_layer_ids=allowed_layer_ids)
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
