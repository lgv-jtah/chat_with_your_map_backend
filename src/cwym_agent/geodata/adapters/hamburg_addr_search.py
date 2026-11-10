"""Geocoder adapter for Hamburg's address search (profile adapter
``hamburg_addr_search``).

Resolves a free-text location string (district, street, address) to
coordinates via Hamburg's public, unauthenticated Elasticsearch
address-search endpoint (``https://api.hamburg.de/addr_search``, a
search template).

This is a much lighter alternative to the ``HamburgAddressResolver``
Haystack component found in the sister `udp-chatbot` project: that
component returns a full ``geopandas.GeoDataFrame`` (via
``shapely``/``pyproj``) for downstream geoprocessing. CWYM only needs a
map center plus, for district/county-level hits, an area geometry to
draw/spatially restrict by (see ``cwym-agent-architecture.md`` Section
9) - both come directly off the same raw JSON response as plain WGS84
GeoJSON, no geometry library needed. The area geometry is only a
rectangle; ``LocationResolver`` replaces it with the official outline.

Which raw ``properties.type`` values count as areas (``broad_types``)
and how they map to CWYM location kinds (``place_types``) comes from the
city profile. Verified live (ImplementationLog.md): "county" is a Bezirk
(the 7 boroughs, e.g. "Altona", "Hamburg-Harburg"), "district" a
Stadtteil (e.g. "Altona-Altstadt", "Ottensen" - distinct from same-named
Bezirke, e.g. "Eimsbüttel" has both). "state"/"city" have no observed
hits and no clean equivalent, so they stay unmapped (kind=None).
"""

import json
import logging
from collections.abc import Collection, Mapping

import httpx

from cwym_agent.geodata.types import LocationMatch, bbox_of

logger = logging.getLogger(__name__)

# Elasticsearch relevance scores from this endpoint aren't bounded to
# [0, 1]; typical strong matches score well above this, so we use it as
# a rough normalization ceiling for a presentable confidence value.
_SCORE_NORMALIZATION_CEILING = 100.0

# properties.geographicextent is a Polygon/MultiPolygon for district/
# county/city-level hits, but the same field is just another Point for
# street-level hits (same shape as properties.geometry) - only these
# types are trustworthy as an area boundary.
_AREA_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})

_SEARCH_TEMPLATE_ID = "addr_search"


class HamburgAddrSearchGeocoder:
    """``Geocoder`` backed by Hamburg's address search."""

    def __init__(
        self,
        url: str,
        broad_types: Collection[str],
        place_types: Mapping[str, str],
        http_client: httpx.Client,
        result_size: int = 20,
    ):
        """
        :param broad_types: raw place types that count as areas. The
            index mixes very different granularities in one result set -
            individual streets score highest for "Wandsbek" (matching many
            "Wandsbeker ..." street names) although the user almost always
            means the borough itself - so area hits are preferred.
        :param place_types: raw place type -> CWYM location kind.
        """

        self.url = url
        self.broad_types = frozenset(t.lower() for t in broad_types)
        self.place_types = {raw.lower(): kind for raw, kind in place_types.items()}
        self.result_size = result_size
        self._client = http_client

    def search(self, query: str) -> LocationMatch | None:
        source_params = {
            "id": _SEARCH_TEMPLATE_ID,
            "params": {
                "query_str": query,
                "from": 0,
                "size": self.result_size,
            },
        }

        try:
            response = self._client.get(
                self.url,
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
        match = self._to_location_match(best_hit, fallback_name=query)
        if match is not None:
            match.alternatives = [name for name in self._ambiguous_names(hits, query) if name != match.name]
        return match

    @staticmethod
    def _hit_type(hit: dict) -> str:
        return (((hit.get("_source") or {}).get("properties") or {}).get("type") or "").lower()

    @staticmethod
    def _hit_name(hit: dict) -> str:
        return (((hit.get("_source") or {}).get("properties") or {}).get("searchfield") or "").strip()

    def _ambiguous_names(self, hits: list[dict], query: str) -> list[str]:
        """Area names the query could equally mean: no area is named
        exactly like the query, but several start with it - "Hoheluft" ->
        Hoheluft-Ost, Hoheluft-West; "Barmbek" -> Barmbek-Nord, Barmbek-Süd.
        Verified live: "Altona", "Eimsbüttel", "Harburg", "Hamm" all have an
        exact area hit and are not ambiguous. Empty if not ambiguous."""

        normalized_query = query.strip().lower()
        names = []
        for hit in hits:
            if self._hit_type(hit) not in self.broad_types:
                continue
            name = self._hit_name(hit)
            if name.lower() == normalized_query:
                return []
            if name.lower().startswith((f"{normalized_query}-", f"{normalized_query} ")) and name not in names:
                names.append(name)
        return names if len(names) >= 2 else []

    def _select_best_hit(self, hits: list[dict], query: str) -> dict:
        """Pick the most useful hit for a map-centering query.

        Area-level hits are preferred over individual streets or
        addresses, since a query like "Wandsbek" almost always means the
        borough, not one of the dozens of "Wandsbeker ..." streets that
        otherwise dominate by raw Elasticsearch score. An exact
        (case-insensitive) name match at that broad level wins outright;
        otherwise the highest-scoring broad hit is used; only if there
        are no broad hits at all do we fall back to the single
        highest-scoring hit of any type (e.g. a genuine street/address
        query).
        """

        def hit_score(hit: dict) -> float:
            return hit.get("_score") or 0.0

        normalized_query = query.strip().lower()
        broad_hits = [hit for hit in hits if self._hit_type(hit) in self.broad_types]

        exact_broad_hits = [hit for hit in broad_hits if self._hit_name(hit).lower() == normalized_query]
        if exact_broad_hits:
            return max(exact_broad_hits, key=hit_score)
        if broad_hits:
            return max(broad_hits, key=hit_score)
        return hits[0]

    def _to_location_match(self, hit: dict, fallback_name: str) -> LocationMatch | None:
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
        boundary = self._extract_boundary(properties)

        return LocationMatch(
            name=name,
            latitude=latitude,
            longitude=longitude,
            confidence=confidence,
            boundary=boundary,
            extent=bbox_of(boundary) if boundary else None,
            kind=self.place_types.get(self._hit_type(hit)),
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
