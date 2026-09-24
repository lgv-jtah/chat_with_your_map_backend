"""Lightweight Hamburg address/district resolver.

Resolves a free-text location string (district, street, or "Hamburg"
itself) to coordinates via Hamburg's public, unauthenticated
Elasticsearch address-search endpoint
(``https://api.hamburg.de/addr_search``).

This is a much lighter alternative to the ``HamburgAddressResolver``
Haystack component found in the sister `udp-chatbot` project: that
component returns a full ``geopandas.GeoDataFrame`` (via
``shapely``/``pyproj``) for downstream geoprocessing. CWYM only needs a
map center plus, for district/city/county-level hits, an area boundary
polygon to draw/spatially restrict by (see ``cwym-agent-architecture.md``
Section 9) - both come directly off the same raw JSON response as plain
WGS84 GeoJSON, no geometry library needed.
"""

import json
import logging

import httpx

from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import LocationMatch

logger = logging.getLogger(__name__)

# Elasticsearch relevance scores from this endpoint aren't bounded to
# [0, 1]; typical strong matches score well above this, so we use it as
# a rough normalization ceiling for a presentable confidence value.
_SCORE_NORMALIZATION_CEILING = 100.0

# Hamburg's address index mixes very different granularities in one
# result set - individual streets score highest for a query like
# "Wandsbek" (matching many "Wandsbeker ..." street names) even though
# the user almost always means the district/borough itself. These are
# the "broad" (non-street/address) location types worth preferring.
_BROAD_TYPES = frozenset({"state", "city", "county", "district"})

# properties.geographicextent is a Polygon/MultiPolygon for district/
# county/city-level hits, but the same field is just another Point for
# street-level hits (same shape as properties.geometry) - only these
# types are trustworthy as an area boundary.
_AREA_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})


class AddressResolver:
    """Resolves Hamburg location strings to coordinates."""

    def __init__(self, settings: GeodataSettings | None = None, http_client: httpx.Client | None = None):
        self.settings = settings or GeodataSettings()
        self._client = http_client or httpx.Client(timeout=self.settings.timeout)

    def resolve(self, query: str) -> LocationMatch | None:
        """Resolve ``query`` to the best-matching location, if any."""

        source_params = {
            "id": "addr_search",
            "params": {
                "query_str": query,
                "from": 0,
                "size": self.settings.address_result_size,
            },
        }

        try:
            response = self._client.get(
                self.settings.address_search_url,
                params={
                    "source": json.dumps(source_params),
                    "source_content_type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Address search request failed for %r: %s", query, exc)
            return None

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            logger.info("No address matches for %r", query)
            return None

        best_hit = self._select_best_hit(hits, query)
        return self._to_location_match(best_hit, fallback_name=query)

    @staticmethod
    def _select_best_hit(hits: list[dict], query: str) -> dict:
        """Pick the most useful hit for a map-centering query.

        District/city/county/state-level hits are preferred over
        individual streets or addresses, since a query like "Wandsbek"
        almost always means the district, not one of the dozens of
        "Wandsbeker ..." streets that otherwise dominate by raw
        Elasticsearch score. An exact (case-insensitive) name match at
        that broad level wins outright; otherwise the highest-scoring
        broad hit is used; only if there are no broad hits at all do we
        fall back to the single highest-scoring hit of any type (e.g. a
        genuine street/address query).
        """

        def hit_type(hit: dict) -> str:
            return ((hit.get("_source") or {}).get("properties") or {}).get("type", "").lower()

        def hit_name(hit: dict) -> str:
            return (((hit.get("_source") or {}).get("properties") or {}).get("searchfield") or "").strip().lower()

        def hit_score(hit: dict) -> float:
            return hit.get("_score") or 0.0

        normalized_query = query.strip().lower()
        broad_hits = [hit for hit in hits if hit_type(hit) in _BROAD_TYPES]

        exact_broad_hits = [hit for hit in broad_hits if hit_name(hit) == normalized_query]
        if exact_broad_hits:
            return max(exact_broad_hits, key=hit_score)
        if broad_hits:
            return max(broad_hits, key=hit_score)
        return hits[0]

    @staticmethod
    def _to_location_match(hit: dict, fallback_name: str) -> LocationMatch | None:
        source = hit.get("_source") or {}
        geometry = source.get("geometry") or {}
        coordinates = geometry.get("coordinates")

        if not coordinates or len(coordinates) < 2:
            logger.warning("Address hit has no usable coordinates: %r", hit)
            return None

        longitude, latitude = coordinates[0], coordinates[1]
        properties = source.get("properties") or {}
        name = properties.get("searchfield") or fallback_name

        score = hit.get("_score") or 0.0
        confidence = min(score / _SCORE_NORMALIZATION_CEILING, 1.0)
        boundary = AddressResolver._extract_boundary(properties)

        return LocationMatch(
            name=name,
            latitude=latitude,
            longitude=longitude,
            confidence=confidence,
            boundary=boundary,
            extent=AddressResolver._bbox_of(boundary) if boundary else None,
        )

    @staticmethod
    def _extract_boundary(properties: dict) -> dict | None:
        """Extract a district/city/county-level area boundary, if any.

        ``properties.geographicextent`` carries a Polygon/MultiPolygon for
        broad-level hits, but the same field is just another Point for
        street-level hits - only the former is a usable boundary. Any
        nonstandard ``crs`` member is stripped so the wire payload is
        plain GeoJSON, as the MapCommand schema promises.
        """

        extent = properties.get("geographicextent")
        if not isinstance(extent, dict) or extent.get("type") not in _AREA_GEOMETRY_TYPES:
            return None

        return {"type": extent["type"], "coordinates": extent.get("coordinates")}

    @staticmethod
    def _bbox_of(boundary: dict) -> tuple[float, float, float, float]:
        """WGS84 (minLon, minLat, maxLon, maxLat) covering a GeoJSON
        Polygon/MultiPolygon - for ZOOM_TO_EXTENT, alongside `boundary`
        itself (which DRAW_BOUNDARY outlines directly)."""

        def points(coords):
            if isinstance(coords[0], (int, float)):
                yield coords
                return
            for item in coords:
                yield from points(item)

        lons, lats = zip(*points(boundary["coordinates"]))
        return min(lons), min(lats), max(lons), max(lats)
