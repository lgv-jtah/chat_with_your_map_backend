"""Scopes ``LayerSearchService``'s search space to what a specific
Masterportal instance actually exposes (``cwym-agent-architecture.md``
Section 8).

``RealDataServices.resolve_layer`` searches Hamburg's full ~6,500-layer
catalog unscoped by default - correct for finding *any* Hamburg layer,
but wrong for a specific portal instance that only registers a small
curated subset (e.g. ``chat_with_your_map_portal``'s ~30 layers):
resolving to a layer outside that subset produces an ``ACTIVATE_LAYER``
``MapCommand`` the frontend can't execute (``Modules/LayerSelection``'s
``changeVisibility`` needs the layer already present in that portal's
own ``config.json``/``layerConfig``, and throws otherwise).

**Design (deviates from the architecture doc in one respect, noted
below):** the frontend forwards its own already-resolved
``config.json`` URL with each query (``portal_context.config_json_url``)
- it already resolved this to boot itself, matching Section 8's stated
principle that the backend never re-derives portal URLs on its own.
``PortalCatalogResolver`` fetches that ``config.json`` and extracts
every layer id declared in ``layerConfig.baselayer``/``subjectlayer``
- the portal's own declaration of which layers it exposes.
``LayerSearchService.search()`` then filters its existing single,
already-embedded Hamburg-wide index down to just those ids (a Haystack
document-store ``filters`` query), rather than building a second,
portal-scoped embedding index as the doc's literal proposal describes.
This reuses the one expensive warm-up (fetch + embed ~6,500 documents)
across every portal instead of repeating it per portal, and since no
embedding step is needed for a ``config.json`` this small, it also
makes onboarding a newly-seen portal cheaper than the doc's own design
at the point that mattered most: cold-start latency for that portal's
first query. ``services_json_url`` is accepted in the request schema
(the frontend already resolves it) but unused for now - this
filter-based design only needs the id set from ``config.json``, not
``services.json``'s fuller per-layer metadata; a future portal needing
metadata not already present in the global Hamburg index would be the
reason to start using it.

Caching is keyed by ``config_json_url`` and refreshed on a plain TTL
(``GeodataSettings.portal_catalog_cache_ttl``) rather than the doc's
ETag/conditional-GET refresh - config.json changes are rare, and a
TTL-only cache is enough for the one real deployment that exists today.
A fetch/parse failure logs a warning and returns ``None`` (unscoped
fallback) rather than failing the whole query - scoping is a
correctness nicety for a specific portal, not something that should
turn a temporary config.json outage into a hard error for every query.

**SSRF hardening.** ``config_json_url`` is client-supplied and fetched
server-side, and ``/api/query`` has no origin restriction
(``allow_origins=["*"]``) - so any web page could otherwise point this
at an internal address. Before fetching, ``_check_url`` rejects
non-http(s) schemes, URLs carrying credentials, and any IP-literal host
that's link-local/private/reserved/multicast (covers the common
concrete targets, e.g. cloud metadata's ``169.254.169.254``) - plus, if
``GeodataSettings.portal_catalog_allowed_hosts`` is configured, any
host outside that allowlist. The response is also capped at
``portal_catalog_max_response_bytes``. A rejected URL is treated the
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
resolved ``config.json`` URL (built from ``window.location``,
mirroring how it boots itself) *is* loopback in that case, and
rejecting it would silently disable scoping for the one setup that
actually exists - confirmed live: the frontend's own dev server
(``webpack-dev-server``, ``localhost``) and this backend running
locally reproduces exactly that. Set this to ``False`` once frontend
and backend are deployed on genuinely separate hosts and loopback is
no longer a legitimate target, alongside tightening CORS.

**TLS verification is skipped for loopback fetches only.**
``webpack-dev-server``'s ``devServer.https`` is hardcoded ``true`` in
this project (both ``npm start`` and ``npm run start:ssl`` serve over
HTTPS with a self-signed certificate) - confirmed live: fetching a
self-signed-cert loopback URL with the default (verifying) client
raises ``SSLCertVerificationError``, caught by the same broad
``except`` as any other fetch failure and silently falling back to
unscoped search, with nothing surfaced to the frontend at all. A
second, separate httpx client with ``verify=False`` is used
specifically when the target is loopback *and* loopback is allowed -
an attacker who can already reach loopback services on this machine
has bigger problems than a spoofed certificate there, so this doesn't
open a new hole beyond what allowing loopback already accepted. A
non-loopback host is always fetched with full certificate
verification, regardless of this setting - this is not a blanket
"skip TLS" switch.
"""

import ipaddress
import logging
import time
from urllib.parse import urlsplit

import httpx

from cwym_agent.geodata.settings import GeodataSettings

logger = logging.getLogger(__name__)


class _URLRejected(Exception):
    """Internal only - caught by get_allowed_layer_ids() and treated like
    any other fetch failure (fail open to unscoped search)."""


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


class PortalCatalogResolver:
    """Resolves the set of layer ids a Masterportal instance exposes."""

    def __init__(
        self,
        settings: GeodataSettings | None = None,
        http_client: httpx.Client | None = None,
        loopback_http_client: httpx.Client | None = None,
    ):
        self.settings = settings or GeodataSettings()
        self._client = http_client or httpx.Client(timeout=self.settings.timeout)
        # A separate, non-certificate-verifying client used only for
        # loopback targets (see the module docstring's "TLS verification"
        # section) - defaults to the same client as an explicitly-passed
        # http_client (e.g. a test's MockTransport, where verify is moot)
        # so existing single-client callers don't need to change.
        self._loopback_client = loopback_http_client or http_client or httpx.Client(
            timeout=self.settings.timeout, verify=False
        )
        self._cache: dict[str, tuple[float, set[str]]] = {}

    def get_allowed_layer_ids(self, config_json_url: str | None) -> set[str] | None:
        """Return the layer ids ``config_json_url``'s portal exposes.

        Returns ``None`` - meaning "don't scope, search unscoped" - when
        no URL was given, or when fetching/parsing it failed.
        """

        if not config_json_url:
            return None

        cached = self._cache.get(config_json_url)
        if cached is not None and time.monotonic() - cached[0] < self.settings.portal_catalog_cache_ttl:
            return cached[1]

        try:
            is_loopback = _check_url(
                config_json_url,
                self.settings.portal_catalog_allowed_hosts,
                self.settings.portal_catalog_allow_loopback,
            )
            client = self._loopback_client if is_loopback else self._client

            response = client.get(config_json_url)
            response.raise_for_status()

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > self.settings.portal_catalog_max_response_bytes:
                raise _URLRejected(f"response too large: {content_length} bytes")
            if len(response.content) > self.settings.portal_catalog_max_response_bytes:
                raise _URLRejected(f"response too large: {len(response.content)} bytes")

            layer_ids = _extract_layer_ids(response.json())
        except (httpx.HTTPError, ValueError, _URLRejected) as exc:
            logger.warning(
                "Failed to fetch/parse config.json at %s (%s); falling back to "
                "unscoped layer search for this query",
                config_json_url,
                exc,
            )
            return None

        self._cache[config_json_url] = (time.monotonic(), layer_ids)
        return layer_ids
