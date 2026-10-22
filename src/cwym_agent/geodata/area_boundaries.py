"""Official outlines of Hamburg's boroughs (Bezirke) and districts
(Stadtteile), from the ``verwaltungsgrenzen`` OGC API Features dataset.

Found live: the "boundary" Hamburg's address search returns for an area
is only its bounding rectangle (5 vertices). Filtering with it picked up
neighbouring areas - "Grundschulen in Eimsbüttel" returned 49 schools
including some in Eppendorf (Hamburg-Nord), against 31 actually in the
borough. The address resolver therefore replaces that rectangle with the
official outline when one exists.

Outlines are slightly simplified (~5 m) - the full ones have thousands
of vertices (Wandsbek: 5,327) - and cached per name; boundaries change
very rarely.
"""

import json
import logging
import time

import httpx
from shapely.geometry import mapping, shape

from cwym_agent.geodata.portal_catalog_resolver import name_key
from cwym_agent.geodata.settings import GeodataSettings

logger = logging.getLogger(__name__)

# LocationMatch.kind -> (collection, name field)
_COLLECTIONS = {
    "bezirk": ("bezirke", "bezirk_name"),
    "stadtteil": ("stadtteile", "stadtteil_name"),
}
_SIMPLIFY_TOLERANCE = 0.00005  # degrees, ~3-5 m in Hamburg
_CACHE_TTL = 24 * 3600.0


class AreaBoundaries:
    def __init__(self, settings: GeodataSettings | None = None, http_client: httpx.Client | None = None):
        self.settings = settings or GeodataSettings()
        self._client = http_client or httpx.Client(timeout=self.settings.timeout)
        self._names: dict[str, tuple[float, dict[str, str]]] = {}
        self._outlines: dict[tuple[str, str], tuple[float, dict | None]] = {}

    def outline(self, name: str, kind: str | None) -> dict | None:
        """The official outline (GeoJSON, WGS84) of the borough/district
        ``name``, or None if there is none (other kinds, unknown name,
        service error) - the caller then keeps the address search's
        rectangle."""

        if kind not in _COLLECTIONS or not name:
            return None
        cache_key = (kind, name_key(name))
        cached = self._outlines.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]

        official = self._official_name(name, kind)
        outline = self._fetch(official, kind) if official else None
        self._outlines[cache_key] = (time.monotonic(), outline)
        return outline

    def _official_name(self, name: str, kind: str) -> str | None:
        """Matches the address search's spelling to the dataset's: the same
        names except for a "Hamburg-" prefix on some boroughs (address
        search "Hamburg-Harburg" = dataset "Harburg")."""

        names = self._name_index(kind)
        for candidate in (name, name.removeprefix("Hamburg-")):
            official = names.get(name_key(candidate))
            if official:
                return official
        return None

    def _name_index(self, kind: str) -> dict[str, str]:
        cached = self._names.get(kind)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]

        collection, field = _COLLECTIONS[kind]
        data = self._get(
            f"{collection}/items", {"f": "json", "limit": 500, "skipGeometry": "true", "properties": field}
        )
        index = {
            name_key(feature["properties"][field]): feature["properties"][field]
            for feature in (data or {}).get("features") or []
            if isinstance(feature.get("properties", {}).get(field), str)
        }
        if index:
            self._names[kind] = (time.monotonic(), index)
        return index

    def _fetch(self, official: str, kind: str) -> dict | None:
        collection, field = _COLLECTIONS[kind]
        escaped = official.replace("'", "''")
        data = self._get(
            f"{collection}/items",
            {"f": "json", "limit": 10, "filter-lang": "cql2-text", "filter": f"{field}='{escaped}'"},
        )
        features = (data or {}).get("features") or []
        if not features or not features[0].get("geometry"):
            logger.info("No official outline found for %s %r", kind, official)
            return None
        try:
            simplified = shape(features[0]["geometry"]).simplify(_SIMPLIFY_TOLERANCE, preserve_topology=True)
        except (ValueError, TypeError) as exc:
            logger.warning("Unusable outline for %s %r (%s)", kind, official, exc)
            return None
        # tuples -> lists, so it is plain JSON-shaped GeoJSON like the rest
        return json.loads(json.dumps(mapping(simplified)))

    def _get(self, path: str, params: dict) -> dict | None:
        try:
            response = self._client.get(f"{self.settings.area_boundaries_url.rstrip('/')}/collections/{path}", params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Area boundary request failed (%s)", exc)
            return None
