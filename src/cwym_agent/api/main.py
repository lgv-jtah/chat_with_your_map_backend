"""FastAPI application exposing the CWYM-Agent pipeline over AG-UI/SSE.

The endpoint accepts a standard AG-UI ``RunAgentInput`` body
(cmym-agent-architecture.md Section 10): a per-chat-session
``threadId``, the last few chat turns, the live map state, and the
portal's layers in ``forwardedProps.portal_context`` (either inline as
``layers`` or as config/services URLs - see ``PortalContext``). Replaces an earlier custom
``QueryRequest`` body that carried only the current message text and a
fresh ``threadId`` per request - with no conversation memory at all, a
follow-up like "zeig mir nur die Gymnasien" after "wo finde ich alle
Schulen" was planned in total isolation (see ImplementationLog.md).
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from functools import lru_cache

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from haystack import Pipeline
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.geodata import clarification
from cwym_agent.geodata.conversation_context import build_context, current_query
from cwym_agent.geodata.portal_catalog_resolver import PortalCatalogResolver, build_inline_catalog
from cwym_agent.geodata.settings import GeodataSettings
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
# origin than this backend. Defaults to all origins (local/dev
# integration); on a shared server set CWYM_CORS_ALLOW_ORIGINS to a
# comma-separated list of the portal origins that may call it.
def _cors_allow_origins() -> list[str]:
    raw = os.environ.get("CWYM_CORS_ALLOW_ORIGINS", "*")
    return [origin.strip() for origin in raw.split(",") if origin.strip()] or ["*"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


class PortalLayer(BaseModel):
    """One entry of ``portal_context.layers`` - a layer the frontend's
    portal exposes, taken from Masterportal's merged ``allLayerConfigs``
    (services.json entry + config.json overrides). Only the fields the
    backend uses are kept; any others are ignored."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=500)
    typ: str | None = Field(default=None, max_length=50)
    url: str | None = Field(default=None, max_length=2000)
    collection: str | None = Field(default=None, max_length=500)
    featureType: str | None = Field(default=None, max_length=200)
    featureNS: str | None = Field(default=None, max_length=500)

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_numeric_id(cls, value):
        # Masterportal layer ids are strings, but a hand-written
        # config may carry plain numbers - accept those too.
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value


class PortalContext(BaseModel):
    """Which layers the requesting portal exposes, in one of two modes
    (exactly one must be used):

    - **inline** (preferred): ``layers`` - the list the frontend already
      resolved itself. Needs no network access from the backend to the
      portal, so it also works for a portal running on a developer's
      ``localhost`` against a shared backend.
    - **URL** (fallback): ``config_json_url`` + ``services_json_url`` -
      fetched server-side by ``PortalCatalogResolver``
      (cmym-agent-architecture.md Section 8). Only works if the backend
      can reach the portal.
    """

    layers: list[PortalLayer] | None = Field(default=None, min_length=1)
    config_json_url: str | None = None
    services_json_url: str | None = None
    languages: list[str] = ["de"]

    @model_validator(mode="after")
    def _exactly_one_mode(self):
        has_urls = bool(self.config_json_url or self.services_json_url)
        if self.layers is not None:
            if has_urls:
                raise ValueError(
                    "portal_context: send either 'layers' or the config/services URLs, not both"
                )
            return self
        if not (self.config_json_url and self.services_json_url):
            raise ValueError(
                "portal_context: needs either 'layers' or both 'config_json_url' and 'services_json_url'"
            )
        return self


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
    if portal_context is not None and portal_context.layers is not None:
        max_layers = GeodataSettings().portal_context_max_layers
        if len(portal_context.layers) > max_layers:
            raise HTTPException(
                status_code=400,
                detail=f"portal_context.layers has {len(portal_context.layers)} entries, max is {max_layers}",
            )
        catalog = build_inline_catalog(layer.model_dump() for layer in portal_context.layers)
    elif portal_context is not None:
        catalog = await portal_catalog_resolver.get_or_build(
            portal_context.config_json_url, portal_context.services_json_url
        )
    allowed_layer_ids = list(catalog.names()) if catalog is not None else None

    try:
        context = build_context(inp, catalog)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # "Hoheluft-West" as the answer to "meinst du Hoheluft-Ost oder
    # Hoheluft-West?" becomes the complete original question again.
    resolved_query = clarification.resolve_reply(query, context.history)
    if resolved_query != query:
        logger.info("Clarification reply %r resolved to %r", query, resolved_query)
        query = resolved_query

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
