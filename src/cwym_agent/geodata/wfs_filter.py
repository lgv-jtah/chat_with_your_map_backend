"""WFS 2.0 support for ``FilterAgent``: the same "attribute conditions AND
spatial intersect" filter it runs against OGC API Features (CQL2), here
as an OGC Filter Encoding (FES 2.0) ``GetFeature`` request.

Most layers in a Masterportal are WFS, not OAF - e.g. Hamburg's school
layers (``HH_WFS_Schulen``). Without this, a query like "Grundschulen in
Hoheluft" fell back to showing the unfiltered layer.

Verified live against ``https://geodienste.hamburg.de/HH_WFS_Schulen``
(deegree):

- The feature type's namespace must be taken from the WFS's own schema
  (``DescribeFeatureType`` ``targetNamespace``): services.json's
  ``featureNS`` can be outdated (``http://www.deegree.org/de.hh.up`` vs.
  the real ``https://registry.gdi-de.org/id/de.hh.up``) - a property name
  in the wrong namespace is rejected. Unprefixed property names
  (``kapitelbezeichnung``) are accepted.
- ``srsName="EPSG:4326"`` gives lon/lat order in both the filter geometry
  and the GeoJSON output (the ``urn:ogc:def:crs:EPSG::4326`` form returns
  lat/lon, which isn't valid GeoJSON).
- ``outputFormat=application/geo+json`` works for results, but not for
  ``resultType=hits`` - the total count is requested in the default (GML)
  format and read from its ``numberMatched`` attribute.

Field names come from ``DescribeFeatureType``, typical values from a
sample of features (so the LLM can map "Grundschulen" to
``kapitelbezeichnung = "Grundschulen"``). Both are cached per layer.
Conditions are compiled deterministically to XML with escaping - the LLM
never writes filter XML itself (same reasoning as the CQL2 path).
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

import httpx

from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import FilterCondition

logger = logging.getLogger(__name__)

_XS = "{http://www.w3.org/2001/XMLSchema}"
_SRS = "EPSG:4326"
# Big enough for every category to show up: found live, the schools WFS
# returns its features sorted by school type, so the first 200 of 451 had
# no "Gymnasien" at all and the LLM couldn't map "Gymnasien" to a value.
_SAMPLE_SIZE = 1000
_MAX_EXAMPLE_VALUES = 12
_MAX_EXAMPLE_LENGTH = 60

_COMPARISON_ELEMENTS = {
    "eq": "PropertyIsEqualTo",
    "ne": "PropertyIsNotEqualTo",
    "lt": "PropertyIsLessThan",
    "lte": "PropertyIsLessThanOrEqualTo",
    "gt": "PropertyIsGreaterThan",
    "gte": "PropertyIsGreaterThanOrEqualTo",
}


@dataclass
class WfsSchema:
    """What FilterAgent needs to know about one WFS feature type."""

    namespace: str
    geometry_field: str | None
    fields: dict[str, str]
    """Non-geometry property name -> XSD type (e.g. "string", "integer")."""
    examples: dict[str, list[str]] = field(default_factory=dict)
    """Property name -> a few distinct values seen in a sample, only for
    fields with a small set of short values (categories like a school
    type), which is what lets the LLM pick the exact stored value."""


def _local_type(xsd_type: str | None) -> str:
    return (xsd_type or "string").split(":")[-1]


def parse_feature_type_schema(xsd: str, feature_type: str) -> WfsSchema | None:
    """Reads the property list of ``feature_type`` from a
    ``DescribeFeatureType`` response. The response may describe several
    feature types (deegree returns all types of the service), so the
    properties are taken from that type's own element only."""

    try:
        root = ET.fromstring(xsd)
    except ET.ParseError:
        return None

    namespace = root.get("targetNamespace") or ""
    element = next(
        (e for e in root.findall(f"{_XS}element") if e.get("name") == feature_type), None
    )
    if element is None:
        return None

    complex_type = element.find(f"{_XS}complexType")
    if complex_type is None and element.get("type"):
        type_name = element.get("type").split(":")[-1]
        complex_type = next(
            (c for c in root.findall(f"{_XS}complexType") if c.get("name") == type_name), None
        )
    if complex_type is None:
        return None

    fields: dict[str, str] = {}
    geometry_field = None
    for prop in complex_type.iter(f"{_XS}element"):
        name = prop.get("name")
        xsd_type = prop.get("type") or ""
        if not name:
            continue
        if xsd_type.startswith("gml:") and xsd_type.endswith("PropertyType"):
            geometry_field = geometry_field or name
            continue
        fields[name] = _local_type(xsd_type)

    return WfsSchema(namespace=namespace, geometry_field=geometry_field, fields=fields)


def collect_examples(features: list[dict], fields: dict[str, str]) -> dict[str, list[str]]:
    """Distinct values per string field of a feature sample - kept only
    for fields that look like categories (a few short values)."""

    values: dict[str, set[str]] = {}
    for feature in features:
        for name, value in (feature.get("properties") or {}).items():
            if fields.get(name) == "string" and isinstance(value, str) and value.strip():
                values.setdefault(name, set()).add(value.strip())

    return {
        name: sorted(found)
        for name, found in values.items()
        if 1 < len(found) <= _MAX_EXAMPLE_VALUES and all(len(v) <= _MAX_EXAMPLE_LENGTH for v in found)
    }


def _literal(value) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return escape(value)
    return None


def _escape_like(value: str) -> str:
    """Escapes the PropertyIsLike wildcard characters used below."""

    return re.sub(r"([*?\\])", r"\\\1", value)


def condition_to_fes(condition: FilterCondition) -> str | None:
    """One extracted condition as an FES 2.0 comparison, or None if it
    can't be represented (unknown operator, unsupported value type)."""

    reference = f"<fes:ValueReference>{escape(condition.field)}</fes:ValueReference>"

    if condition.operator == "contains":
        if not isinstance(condition.value, (str, int, float)) or isinstance(condition.value, bool):
            return None
        pattern = escape(_escape_like(str(condition.value)))
        return (
            '<fes:PropertyIsLike wildCard="*" singleChar="?" escapeChar="\\" matchCase="false">'
            f"{reference}<fes:Literal>*{pattern}*</fes:Literal></fes:PropertyIsLike>"
        )

    element = _COMPARISON_ELEMENTS.get(condition.operator)
    literal = _literal(condition.value)
    if element is None or literal is None:
        return None
    match_case = ' matchCase="false"' if isinstance(condition.value, str) else ""
    return f"<fes:{element}{match_case}>{reference}<fes:Literal>{literal}</fes:Literal></fes:{element}>"


def _pos_list(ring: list[list[float]]) -> str:
    return " ".join(f"{lon} {lat}" for lon, lat, *_ in ring)


def _gml_polygon(rings: list[list[list[float]]], gml_id: str) -> str:
    exterior, *interiors = rings
    parts = [
        f"<gml:exterior><gml:LinearRing><gml:posList>{_pos_list(exterior)}</gml:posList>"
        "</gml:LinearRing></gml:exterior>"
    ]
    parts += [
        f"<gml:interior><gml:LinearRing><gml:posList>{_pos_list(ring)}</gml:posList>"
        "</gml:LinearRing></gml:interior>"
        for ring in interiors
    ]
    return f'<gml:Polygon gml:id="{gml_id}" srsName="{_SRS}">{"".join(parts)}</gml:Polygon>'


def boundary_to_gml(boundary: dict) -> str | None:
    """A GeoJSON Polygon/MultiPolygon (WGS84, lon/lat) as GML 3.2."""

    coordinates = boundary.get("coordinates")
    if not coordinates:
        return None
    if boundary.get("type") == "Polygon":
        return _gml_polygon(coordinates, "cwym_area")
    if boundary.get("type") == "MultiPolygon":
        members = "".join(
            f"<gml:surfaceMember>{_gml_polygon(polygon, f'cwym_area_{index}')}</gml:surfaceMember>"
            for index, polygon in enumerate(coordinates)
        )
        return f'<gml:MultiSurface gml:id="cwym_areas" srsName="{_SRS}">{members}</gml:MultiSurface>'
    return None


def build_filter(clauses: list[str]) -> str:
    if not clauses:
        return ""
    body = clauses[0] if len(clauses) == 1 else f"<fes:And>{''.join(clauses)}</fes:And>"
    return f"<fes:Filter>{body}</fes:Filter>"


def build_get_feature(
    feature_type: str, namespace: str, filter_xml: str, count: int, hits: bool = False
) -> str:
    """A WFS 2.0 GetFeature request body (POST)."""

    output = 'resultType="hits"' if hits else f'count="{int(count)}" outputFormat="application/geo+json"'
    return (
        f'<wfs:GetFeature service="WFS" version="2.0.0" {output} '
        'xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:fes="http://www.opengis.net/fes/2.0" '
        f'xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:app={quoteattr(namespace)}>'
        f'<wfs:Query typeNames={quoteattr("app:" + feature_type)} srsName="{_SRS}">'
        f"{filter_xml}</wfs:Query></wfs:GetFeature>"
    )


_NUMBER_MATCHED = re.compile(r'numberMatched="(\d+)"')


class WfsClient:
    """Schema discovery (cached) and filtered GetFeature requests."""

    def __init__(self, settings: GeodataSettings | None = None, http_client: httpx.Client | None = None):
        self.settings = settings or GeodataSettings()
        self._client = http_client or httpx.Client(timeout=self.settings.timeout)
        self._cache: dict[str, tuple[float, WfsSchema]] = {}

    def schema(self, url: str, feature_type: str) -> WfsSchema | None:
        cache_key = f"{url}|{feature_type}"
        cached = self._cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < self.settings.filter_queryables_cache_ttl:
            return cached[1]

        try:
            response = self._client.get(
                url,
                params={
                    "SERVICE": "WFS",
                    "VERSION": "2.0.0",
                    "REQUEST": "DescribeFeatureType",
                    "TYPENAMES": feature_type,
                },
            )
            response.raise_for_status()
            schema = parse_feature_type_schema(response.text, feature_type)
        except httpx.HTTPError as exc:
            logger.warning("WFS DescribeFeatureType failed for %s %s (%s)", url, feature_type, exc)
            return None
        if schema is None:
            logger.warning("WFS DescribeFeatureType for %s %s had no usable schema", url, feature_type)
            return None

        sample = self._post(url, build_get_feature(feature_type, schema.namespace, "", _SAMPLE_SIZE))
        if sample is not None:
            schema.examples = collect_examples(sample.get("features") or [], schema.fields)

        self._cache[cache_key] = (time.monotonic(), schema)
        return schema

    def get_features(self, url: str, feature_type: str, namespace: str, filter_xml: str, limit: int):
        """Returns ``(GeoJSON dict, numberMatched or None)``, or None on failure."""

        data = self._post(url, build_get_feature(feature_type, namespace, filter_xml, limit))
        if data is None:
            return None

        number_matched = data.get("numberMatched")
        if number_matched is None and len(data.get("features") or []) >= limit:
            number_matched = self._count(url, feature_type, namespace, filter_xml)
        return data, number_matched

    def _count(self, url: str, feature_type: str, namespace: str, filter_xml: str) -> int | None:
        try:
            response = self._client.post(
                url,
                content=build_get_feature(feature_type, namespace, filter_xml, 0, hits=True),
                headers={"Content-Type": "text/xml"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        match = _NUMBER_MATCHED.search(response.text)
        return int(match.group(1)) if match else None

    def _post(self, url: str, body: str) -> dict | None:
        try:
            response = self._client.post(url, content=body, headers={"Content-Type": "text/xml"})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("WFS GetFeature failed for %s (%s)", url, exc)
            return None
