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
"""

import logging
import time

import httpx

from cwym_agent.geodata.settings import GeodataSettings

logger = logging.getLogger(__name__)


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

    def __init__(self, settings: GeodataSettings | None = None, http_client: httpx.Client | None = None):
        self.settings = settings or GeodataSettings()
        self._client = http_client or httpx.Client(timeout=self.settings.timeout)
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
            response = self._client.get(config_json_url)
            response.raise_for_status()
            layer_ids = _extract_layer_ids(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Failed to fetch/parse config.json at %s (%s); falling back to "
                "unscoped layer search for this query",
                config_json_url,
                exc,
            )
            return None

        self._cache[config_json_url] = (time.monotonic(), layer_ids)
        return layer_ids
