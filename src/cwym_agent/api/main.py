"""FastAPI application exposing the CWYM-Agent pipeline over AG-UI/SSE.

The endpoint accepts a standard AG-UI ``RunAgentInput`` body
(cmym-agent-architecture.md Section 10): a per-chat-session
``threadId``, the last few chat turns, the live map state, and the
resolved portal config/services URLs in
``forwardedProps.portal_context``. Replaces an earlier custom
``QueryRequest`` body that carried only the current message text and a
fresh ``threadId`` per request - with no conversation memory at all, a
follow-up like "zeig mir nur die Gymnasien" after "wo finde ich alle
Schulen" was planned in total isolation (see ImplementationLog.md).
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from functools import lru_cache

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from haystack import Pipeline
from pydantic import BaseModel, ValidationError

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.geodata.conversation_context import build_context, current_query
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
    (cmym-agent-architecture.md Section 8) - the backend never
    re-derives portal URLs on its own. Both URLs are required now
    (unlike the earlier config.json-only design): PortalCatalogResolver
    cross-references services.json for per-layer metadata FilterAgent
    needs to target an already-active layer (Section 10)."""

    config_json_url: str
    services_json_url: str
    languages: list[str] = ["de"]


@app.post("/api/query")
async def query_endpoint(
    inp: RunAgentInput,
    pipeline: Pipeline = Depends(get_pipeline),
    portal_catalog_resolver: PortalCatalogResolver = Depends(get_portal_catalog_resolver),
):
    try:
        query = current_query(inp)
        raw_portal_context = (inp.forwarded_props or {}).get("portal_context")
        portal_context = PortalContext.model_validate(raw_portal_context) if raw_portal_context else None
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    catalog = None
    if portal_context is not None:
        catalog = await portal_catalog_resolver.get_or_build(
            portal_context.config_json_url, portal_context.services_json_url
        )
    allowed_layer_ids = list(catalog.names()) if catalog is not None else None

    try:
        context = build_context(inp, catalog)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bridge = AGUIEventBridge(thread_id=inp.thread_id, run_id=inp.run_id)
    encoder = EventEncoder()

    def run_pipeline_sync():
        bridge.run_started()
        try:
            run_pipeline(
                pipeline,
                query,
                bridge=bridge,
                allowed_layer_ids=allowed_layer_ids,
                context=context,
                layer_catalog=catalog,
            )
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
            yield encoder.encode(event)

        await task

    return StreamingResponse(event_stream(), media_type=encoder.get_content_type())


@app.get("/health")
async def health():
    return {"status": "ok"}
