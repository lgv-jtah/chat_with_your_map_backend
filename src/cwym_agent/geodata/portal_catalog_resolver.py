"""Scopes the pipeline to what a specific Masterportal instance actually
exposes (``cmym-agent-architecture.md`` Section 8).

``RealDataServices.resolve_layer`` searches Hamburg's full ~6,500-layer
catalog unscoped by default - correct for finding *any* Hamburg layer,
but wrong for a specific portal instance that only registers a small
curated subset (e.g. ``chat_with_your_map_portal``'s ~30 layers):
resolving to a layer outside that subset produces an ``ACTIVATE_LAYER``
``MapCommand`` the frontend can't execute (``Modules/LayerSelection``'s
``changeVisibility`` needs the layer already present in that portal's
own ``config.json``/``layerConfig``, and throws otherwise).

**Design.** The frontend forwards its own already-resolved
``config.json``/``services.json`` URLs with each query
(``portal_context``) - it already resolved these to boot itself,
matching Section 8's stated principle that the backend never re-derives
portal URLs on its own. ``PortalCatalogResolver`` fetches both,
cross-references ``config.json``'s declared layer ids (from
``layerConfig.baselayer``/``subjectlayer``) against ``services.json``'s
per-layer metadata (``id``/``typ``/``name``/``url``/``collection``,
confirmed live against the real ``services-internet.json`` shape), and
returns a ``LayerCatalog``: not just the id *set* (as an earlier,
simpler version of this module did) but per-layer metadata, because
``FilterAgent`` needs to target an already-active layer id (a
conversation-context follow-up, Section 10) without a fresh
``layer_resolver`` vector search to supply that metadata.

**Inline mode (preferred).** The frontend may instead send the already
merged layer list itself (``portal_context.layers``), built into a
catalog by ``build_inline_catalog`` without any fetch. That is the only
mode that works when the backend runs on a shared server that cannot
reach the portal (e.g. a portal on a developer's ``localhost``). The
URL mode below stays as a fallback for publicly hosted portals.

Concurrent cold-start requests for the same ``(config_json_url,
services_json_url)`` pair share one build via a per-key
``asyncio.Lock`` (single-flight) - cheap to add now that this fetch
does two round trips and some parsing instead of one, and matches the
architecture doc's design. Refreshed via a TTL-gated conditional GET
(``ETag``/``If-None-Match``, ``Last-Modified``/``If-Modified-Since``)
per resource URL, reusing the previously parsed body on ``304 Not
Modified`` rather than re-parsing on every TTL expiry.

A fetch/parse failure (either URL) logs a warning and returns ``None``
(unscoped fallback) rather than failing the whole query - scoping is a
correctness nicety for a specific portal, not something that should
turn a temporary config/services outage into a hard error for every
query.

**SSRF hardening.** Both URLs are client-supplied and fetched
server-side, and ``/api/query`` has no origin restriction
(``allow_origins=["*"]``) - so any web page could otherwise point this
at an internal address. Before fetching, ``_check_url`` rejects
non-http(s) schemes, URLs carrying credentials, and any IP-literal host
that's link-local/private/reserved/multicast (covers the common
concrete targets, e.g. cloud metadata's ``169.254.169.254``) - plus, if
``GeodataSettings.portal_catalog_allowed_hosts`` is configured, any
host outside that allowlist. Applied independently to *both* URLs.
Redirects are never followed (``follow_redirects=False``): a URL that
passes the check but redirects to a disallowed target must not be
silently fetched anyway. Each response is capped at
``portal_catalog_max_response_bytes``, checked against both the
``Content-Length`` header (fails fast) and the actual streamed byte
count (catches a missing/lying header). A rejected URL is treated the
same as any other fetch failure: logged and falls back to unscoped
search, not a raised exception. Deliberately does **not** resolve DNS
to catch a domain name that resolves to a private IP (rebinding) -
doing that safely needs a custom httpx transport so the checked IP is
the one actually connected to, not just looked up ahead of time; a
documented scope limit here, not an oversight.

**Loopback/``localhost`` is allowed by default**
(``GeodataSettings.portal_catalog_allow_loopback``, defaulting to
``True``) - deliberately, not an oversight, same reasoning as the
wide-open CORS above: this project's only real deployment today runs
the frontend and backend on the same host, so the frontend's own
resolved URLs (built from ``window.location``, mirroring how it boots
itself) *are* loopback in that case, and rejecting them would silently
disable scoping for the one setup that actually exists - confirmed
live: the frontend's own dev server (``webpack-dev-server``,
``localhost``) and this backend running locally reproduces exactly
that. Set this to ``False`` once frontend and backend are deployed on
genuinely separate hosts and loopback is no longer a legitimate target,
alongside tightening CORS.

**TLS verification is skipped for loopback fetches only.**
``webpack-dev-server``'s ``devServer.https`` is hardcoded ``true`` in
this project (both ``npm start`` and ``npm run start:ssl`` serve over
HTTPS with a self-signed certificate) - confirmed live: fetching a
self-signed-cert loopback URL with the default (verifying) client
raises ``SSLCertVerificationError``, caught by the same broad
``except`` as any other fetch failure and silently falling back to
unscoped search, with nothing surfaced to the frontend at all. A
second, separate ``httpx.AsyncClient`` with ``verify=False`` is used
specifically when the target is loopback *and* loopback is allowed -
an attacker who can already reach loopback services on this machine
has bigger problems than a spoofed certificate there, so this doesn't
open a new hole beyond what allowing loopback already accepted. A
non-loopback host is always fetched with full certificate
verification, regardless of this setting - this is not a blanket
"skip TLS" switch.
"""

import hashlib
import ipaddress
import json
import logging
import time
from asyncio import Lock
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import LayerMatch

logger = logging.getLogger(__name__)


class _URLRejected(Exception):
    """Internal only - caught by get_or_build() and treated like any
    other fetch failure (fail open to unscoped search)."""


_LOOPBACK_HOSTNAMES = frozenset({"localhost"})


def _check_url(url: str, allowed_hosts: frozenset[str], allow_loopback: bool) -> bool:
    """Raises _URLRejected if the URL isn't acceptable at all. Otherwise
    returns whether the host is loopback, so the caller can pick the
    (non-certificate-verifying) loopback client for the actual fetch."""

    parts = urlsplit(url)

    if parts.scheme not in ("http", "https"):
        raise _URLRejected(f"unsupported scheme: {parts.scheme!r}")
    if parts.username or parts.password:
        raise _URLRejected("URL must not carry credentials")

    hostname = parts.hostname
    if not hostname:
        raise _URLRejected("URL has no hostname")

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    is_loopback = hostname in _LOOPBACK_HOSTNAMES or (ip is not None and ip.is_loopback)
    if is_loopback and not allow_loopback:
        raise _URLRejected(f"loopback host not allowed: {hostname}")

    if ip is not None and not is_loopback and (
        ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast
    ):
        raise _URLRejected(f"IP literal not allowed: {hostname}")

    if allowed_hosts and hostname not in allowed_hosts:
        raise _URLRejected(f"host not in allowlist: {hostname}")

    return is_loopback


def _flatten_layer_ids(elements: list[dict]) -> set[str]:
    """``layerConfig.{baselayer,subjectlayer}.elements[].id`` is either a
    single id or (for a WMS/WFS layer group) a list of ids - flatten
    either shape into one set of string ids."""

    ids: set[str] = set()
    for element in elements:
        raw_id = element.get("id")
        if isinstance(raw_id, list):
            ids.update(str(single_id) for single_id in raw_id)
        elif raw_id is not None:
            ids.add(str(raw_id))
    return ids


def _extract_layer_ids(config_json: dict) -> set[str]:
    layer_config = config_json.get("layerConfig") or {}
    ids: set[str] = set()
    for section in ("baselayer", "subjectlayer"):
        elements = (layer_config.get(section) or {}).get("elements") or []
        ids |= _flatten_layer_ids(elements)
    return ids


def _build_layer_catalog(config_json: dict, services_json) -> "LayerCatalog":
    """Cross-references config.json's exposed ids against services.json's
    per-layer metadata - an id declared in config.json but missing from
    services.json (unexpected, but not impossible for a hand-edited
    portal) is silently dropped rather than half-populated."""

    exposed_ids = _extract_layer_ids(config_json)
    if not isinstance(services_json, list):
        raise _URLRejected("services.json is not a list")

    by_id = {str(entry["id"]): entry for entry in services_json if isinstance(entry, dict) and "id" in entry}

    layers: dict[str, LayerMatch] = {}
    for layer_id in exposed_ids:
        entry = by_id.get(layer_id)
        if entry is None:
            continue
        layers[layer_id] = LayerMatch(
            name=entry.get("name") or layer_id,
            layer_id=layer_id,
            confidence=1.0,
            typ=entry.get("typ"),
            url=entry.get("url"),
            collection=entry.get("collection"),
        )
    return LayerCatalog(layers)


_RUNTIME_LAYER_ID_PREFIX = "cwym_"
_RUNTIME_LAYER_TYPES = frozenset({"VECTORBASE"})


def _is_runtime_layer(layer_id: str, typ: object) -> bool:
    return layer_id.startswith(_RUNTIME_LAYER_ID_PREFIX) or (
        isinstance(typ, str) and typ.upper() in _RUNTIME_LAYER_TYPES
    )


def build_inline_catalog(layers: Iterable[Mapping[str, object]]) -> "LayerCatalog":
    """Builds a ``LayerCatalog`` from the layer list the frontend sent
    inline (``portal_context.layers``) instead of from two fetched URLs.

    The Masterportal has already merged config.json with services.json
    in the browser (``allLayerConfigs``), so the frontend can send that
    list directly - the backend then needs no network access to the
    portal at all, which is what makes a shared backend usable with a
    portal running on a developer's own ``localhost``. Each entry needs
    an ``id``; ``name``/``typ``/``url``/``collection`` are optional and
    produce the same ``LayerMatch`` records ``_build_layer_catalog``
    does. Size/field validation happens at the API boundary
    (``api.main.PortalLayer``) before this is called.

    Runtime layers the frontend creates itself - the chat addon's own
    result layers (ids ``cwym_...``) and draw/import layers (typ
    ``VECTORBASE``, in-memory features without a service URL) - are
    dropped: they are not searchable services, and treating them as
    known layers would let them into the planner's context and layer
    scoping. The frontend already leaves them out; this is a safety net."""

    result: dict[str, LayerMatch] = {}
    for entry in layers:
        layer_id = str(entry["id"])
        if _is_runtime_layer(layer_id, entry.get("typ")):
            continue
        result[layer_id] = LayerMatch(
            name=entry.get("name") or layer_id,
            layer_id=layer_id,
            confidence=1.0,
            typ=entry.get("typ"),
            url=entry.get("url"),
            collection=entry.get("collection"),
        )
    return LayerCatalog(result)


@dataclass(frozen=True)
class LayerCatalog:
    """The layer ids + metadata a Masterportal instance actually
    exposes. Consumed by ``LayerSearchService`` (scoping search via the
    id set), ``FilterAgent`` (per-layer metadata for an already-active
    layer id), and the planner context block / NLG (layer names)."""

    _layers: dict[str, LayerMatch] = field(default_factory=dict)

    def has(self, layer_id: str) -> bool:
        return layer_id in self._layers

    def get(self, layer_id: str) -> LayerMatch | None:
        return self._layers.get(layer_id)

    def name(self, layer_id: str) -> str:
        match = self._layers.get(layer_id)
        return match.name if match else layer_id

    def names(self) -> dict[str, str]:
        return {layer_id: match.name for layer_id, match in self._layers.items()}


@dataclass
class _CachedResource:
    """A previously-fetched config.json/services.json body, kept so a
    304 response on refresh can reuse it instead of re-parsing."""

    data: object
    etag: str | None
    last_modified: str | None


class PortalCatalogResolver:
    """Resolves the layer catalog a Masterportal instance exposes."""

    def __init__(
        self,
        settings: GeodataSettings | None = None,
        http_client: httpx.AsyncClient | None = None,
        loopback_http_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings or GeodataSettings()
        self._client = http_client or httpx.AsyncClient(
            follow_redirects=False, timeout=self.settings.timeout
        )
        # A separate, non-certificate-verifying client used only for
        # loopback targets (see the module docstring's "TLS verification"
        # section) - defaults to the same client as an explicitly-passed
        # http_client (e.g. a test's MockTransport, where verify is moot)
        # so single-client callers don't need to pass both.
        self._loopback_client = loopback_http_client or http_client or httpx.AsyncClient(
            follow_redirects=False, timeout=self.settings.timeout, verify=False
        )
        self._resources: dict[str, _CachedResource] = {}
        self._catalogs: dict[str, tuple[float, LayerCatalog]] = {}
        self._locks: dict[str, Lock] = {}

    async def get_or_build(
        self, config_json_url: str | None, services_json_url: str | None
    ) -> LayerCatalog | None:
        """Return the layer catalog ``config_json_url``/``services_json_url``'s
        portal exposes.

        Returns ``None`` - meaning "don't scope, search unscoped" - when
        either URL is missing, or when fetching/parsing either one
        failed.
        """

        if not config_json_url or not services_json_url:
            return None

        key = hashlib.sha256(f"{config_json_url}\n{services_json_url}".encode()).hexdigest()

        cached = self._catalogs.get(key)
        if cached is not None and time.monotonic() - cached[0] < self.settings.portal_catalog_cache_ttl:
            return cached[1]

        # Single-flight: concurrent cold-start requests for the same
        # portal build once. Safe without extra locking around the
        # dict access itself - asyncio is cooperative/single-threaded,
        # so no other coroutine can interleave between this lookup and
        # the setdefault() below.
        lock = self._locks.setdefault(key, Lock())
        async with lock:
            cached = self._catalogs.get(key)
            if cached is not None and time.monotonic() - cached[0] < self.settings.portal_catalog_cache_ttl:
                return cached[1]

            try:
                config_json = await self._fetch_resource(config_json_url)
                services_json = await self._fetch_resource(services_json_url)
                catalog = _build_layer_catalog(config_json, services_json)
            except (httpx.HTTPError, ValueError, TypeError, _URLRejected) as exc:
                logger.warning(
                    "Failed to fetch/parse portal catalog (config=%s, services=%s): %s; "
                    "falling back to unscoped layer search for this query",
                    config_json_url,
                    services_json_url,
                    exc,
                )
                return None

            self._catalogs[key] = (time.monotonic(), catalog)
            return catalog

    async def _fetch_resource(self, url: str):
        is_loopback = _check_url(
            url, self.settings.portal_catalog_allowed_hosts, self.settings.portal_catalog_allow_loopback
        )
        client = self._loopback_client if is_loopback else self._client

        headers = {}
        cached = self._resources.get(url)
        if cached is not None:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified

        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304 and cached is not None:
                return cached.data

            response.raise_for_status()

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > self.settings.portal_catalog_max_response_bytes:
                raise _URLRejected(f"response too large: {content_length} bytes")

            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf += chunk
                if len(buf) > self.settings.portal_catalog_max_response_bytes:
                    raise _URLRejected(
                        f"response too large: over {self.settings.portal_catalog_max_response_bytes} bytes"
                    )

            data = json.loads(buf)

        self._resources[url] = _CachedResource(
            data=data,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
        return data
