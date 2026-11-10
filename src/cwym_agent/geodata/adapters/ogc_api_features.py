"""Boundary source adapter for an OGC API Features dataset of
administrative areas (profile adapter ``ogc_api_features``).

Found live: the area geometry an address search returns is often only a
bounding rectangle (5 vertices). Filtering with it picked up neighbouring
areas - "Grundschulen in Eimsbüttel" returned 49 schools including some
from the next borough, against 31 actually inside. ``LocationResolver``
therefore replaces that rectangle with the official outline from here
when one exists.

The profile maps each location kind to a collection and the property
holding the area name. Outlines are slightly simplified (~5 m at mid
latitudes) - the full ones have thousands of vertices - and cached per
name; boundaries change very rarely.
"""

import json
import logging
import time
from collections.abc import Mapping, Sequence

import httpx
from shapely.geometry import mapping, shape

from cwym_agent.geodata.portal_catalog_resolver import name_key

logger = logging.getLogger(__name__)

_SIMPLIFY_TOLERANCE = 0.00005  # degrees, ~3-5 m at mid latitudes
_CACHE_TTL = 24 * 3600.0


class OgcApiFeaturesBoundaries:
    """``BoundarySource`` backed by OGC API Features collections."""

    def __init__(
        self,
        url: str,
        collections: Mapping[str, tuple[str, str]],
        http_client: httpx.Client,
        strip_name_prefixes: Sequence[str] = (),
    ):
        """
        :param url: the dataset's landing page (``.../collections`` is appended).
        :param collections: location kind -> (collection id, name property).
        :param strip_name_prefixes: prefixes the geocoder puts on some area
            names that the dataset doesn't have (tried after the name as is).
        """

        self.url = url.rstrip("/")
        self.collections = dict(collections)
        self.strip_name_prefixes = tuple(strip_name_prefixes)
        self._client = http_client
        self._names: dict[str, tuple[float, dict[str, str]]] = {}
        self._outlines: dict[tuple[str, str], tuple[float, dict | None]] = {}

    def outline(self, name: str, kind: str) -> dict | None:
        if kind not in self.collections or not name:
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
        """Matches the geocoder's spelling to the dataset's: the same names
        (compared by ``name_key``), possibly without one of
        ``strip_name_prefixes``."""

        names = self._name_index(kind)
        candidates = [name, *(name.removeprefix(p) for p in self.strip_name_prefixes if name.startswith(p))]
        for candidate in candidates:
            official = names.get(name_key(candidate))
            if official:
                return official
        return None

    def _name_index(self, kind: str) -> dict[str, str]:
        cached = self._names.get(kind)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]

        collection, field = self.collections[kind]
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
        collection, field = self.collections[kind]
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
            response = self._client.get(f"{self.url}/collections/{path}", params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Area boundary request failed (%s)", exc)
            return None
