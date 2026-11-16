"""Boundary source adapter for a WFS 2.0 layer of administrative areas
(profile adapter ``wfs``), for cities without an OGC API Features service.

The whole feature collection of a type is fetched once as GeoJSON (area
layers are small), cached for 24 h, and areas are looked up by
``name_key`` of the name property. Several features with the same name
(an area split in two parts) are merged into one outline. Outlines are
slightly simplified like in ``ogc_api_features``.
"""

import json
import logging
import time
from collections.abc import Mapping, Sequence

import httpx
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from cwym_agent.geodata.adapters.ogc_api_features import _CACHE_TTL, _SIMPLIFY_TOLERANCE
from cwym_agent.geodata.portal_catalog_resolver import name_key

logger = logging.getLogger(__name__)

_MAX_FEATURES = 1000


class WfsBoundaries:
    """``BoundarySource`` backed by WFS feature types."""

    def __init__(
        self,
        url: str,
        collections: Mapping[str, tuple[str, str]],
        http_client: httpx.Client,
        strip_name_prefixes: Sequence[str] = (),
    ):
        """
        :param url: the WFS endpoint.
        :param collections: location kind -> (type name incl. prefix, name property).
        :param strip_name_prefixes: prefixes the geocoder puts on some area
            names that the dataset doesn't have (tried after the name as is).
        """

        self.url = url
        self.collections = dict(collections)
        self.strip_name_prefixes = tuple(strip_name_prefixes)
        self._client = http_client
        self._features_cache: dict[str, tuple[float, list[dict]]] = {}
        self._outlines: dict[tuple[str, str], tuple[float, dict | None]] = {}

    def outline(self, name: str, kind: str) -> dict | None:
        if kind not in self.collections or not name:
            return None
        cache_key = (kind, name_key(name))
        cached = self._outlines.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]

        type_name, field = self.collections[kind]
        features = self._features(type_name)
        if not features:
            # a failed fetch must not be remembered as "no such area"
            return None

        by_name: dict[str, list[BaseGeometry]] = {}
        for feature in features:
            value = (feature.get("properties") or {}).get(field)
            geometry = _geometry(feature)
            if isinstance(value, str) and geometry is not None:
                by_name.setdefault(name_key(value), []).append(geometry)

        candidates = [name, *(name.removeprefix(p) for p in self.strip_name_prefixes if name.startswith(p))]
        geometries = next((by_name[k] for c in candidates if (k := name_key(c)) in by_name), [])
        outline = _union_outline(geometries)
        if outline is None:
            logger.info("No official outline found for %s %r", kind, name)
        self._outlines[cache_key] = (time.monotonic(), outline)
        return outline

    def collection_outline(self, collection: str) -> dict | None:
        return _union_outline([g for f in self._features(collection) if (g := _geometry(f)) is not None])

    def _features(self, type_name: str) -> list[dict]:
        cached = self._features_cache.get(type_name)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]

        try:
            response = self._client.get(
                self.url,
                params={
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "GetFeature",
                    "typeNames": type_name,
                    "outputFormat": "application/json",
                    # EPSG:4326 gives lon/lat order here (verified on GeoServer)
                    "srsName": "EPSG:4326",
                    "count": _MAX_FEATURES,
                },
            )
            response.raise_for_status()
            features = response.json().get("features") or []
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("Area boundary request failed for %s (%s)", type_name, exc)
            return []
        if features:
            self._features_cache[type_name] = (time.monotonic(), features)
        return features


def _geometry(feature: dict) -> BaseGeometry | None:
    try:
        return shape(feature["geometry"]) if feature.get("geometry") else None
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("Unusable area geometry (%s)", exc)
        return None


def _union_outline(geometries: Sequence[BaseGeometry]) -> dict | None:
    if not geometries:
        return None
    simplified = unary_union(list(geometries)).simplify(_SIMPLIFY_TOLERANCE, preserve_topology=True)
    # tuples -> lists, so it is plain JSON-shaped GeoJSON like the rest
    return json.loads(json.dumps(mapping(simplified)))
