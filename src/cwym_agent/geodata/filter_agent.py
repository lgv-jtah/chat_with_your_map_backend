"""``FilterAgent``: translates a natural-language filter query into an
OGC API Features (OAF) CQL2 filter and executes it against a layer's
own ``/collections/{id}/items`` endpoint
(``cwym-agent-architecture.md`` Section 9).

Bypasses Masterportal's native Filter tool entirely (see the doc for
why - no OAF support server-side, predefined-query-only design, open
bugs in exactly this territory) and works only against OAF-typed
layers, the one Hamburg service type CQL2 filtering is defined for.

Qwen3 extracts attribute conditions as small structured JSON
(field/operator/value - the same shape ``RealDataServices``' other
LLM-assisted steps already use for classification-style outputs), not
raw CQL2 text: compiling CQL2 deterministically from structured
conditions avoids trusting the model to produce syntactically valid,
correctly-quoted/escaped CQL2 from free text, and rules out a whole
class of injection-via-filter-value bugs a raw-text approach would
otherwise need to defend against. When the query also resolved a
location with an area boundary, a ``S_INTERSECTS(geometry, boundary)``
spatial predicate is ANDed in deterministically (no LLM involvement -
the geometry comes straight from ``AddressResolver``) - this is what
lets ``FilterAgent`` supersede the old client-side ``APPLY_SPATIAL_FILTER``
masking mechanism for "<layer> in <district>"-shaped queries, not just
replace its name. Verified live against Hamburg's real API:
``S_INTERSECTS`` narrowed Hamburg's 37 hospitals down to the 1 actually
within Wandsbek's boundary.

Attribute schema discovery uses OAF's own ``/collections/{id}/queryables``
endpoint (OGC API Features Part 3) rather than guessing field names -
fetched and cached lazily per collection on first filter hit, not
pre-indexed for all ~7,000 layers up front (most will never be
filtered).
"""

import json
import logging
import re
import time
from typing import Any

import httpx

from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import FilterCondition, FilterResult, LayerMatch, LocationMatch

logger = logging.getLogger(__name__)

_CONDITION_EXTRACTION_PROMPT_TEMPLATE = (
    "Du hilfst dabei, aus einer deutschen Filteranfrage strukturierte "
    "Bedingungen für eine Datenabfrage zu extrahieren. Verfügbare Felder "
    "der Kartenebene:\n{fields}\n\n"
    "Anfrage: \"{query}\"\n\n"
    "Antworte NUR mit einem JSON-Array von Bedingungen der Form "
    "{{\"field\": <Feldname>, \"operator\": <eq|ne|lt|lte|gt|gte|contains>, "
    "\"value\": <Wert>}}. Verwende NUR Felder aus der obigen Liste. Wenn "
    "die Anfrage keine konkrete Attributbedingung nennt (z.B. nur einen "
    "Ort), antworte mit einem leeren Array: []"
)

_VALID_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "contains"})

_OPERATOR_TO_CQL2 = {
    "eq": "=",
    "ne": "<>",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
}


def _escape_cql2_string(value: str) -> str:
    """CQL2-text string literals use doubled single quotes, like SQL."""

    return value.replace("'", "''")


def _condition_to_cql2(condition: FilterCondition) -> str | None:
    """Deterministically compile one extracted condition to a CQL2-text
    clause. Returns None for anything that can't be represented (an
    unrecognized operator, or a value of a type CQL2-text has no plain
    literal for) - the caller drops those rather than failing the whole
    filter."""

    if condition.operator == "contains":
        return f"{condition.field} LIKE '%{_escape_cql2_string(str(condition.value))}%'"

    cql2_operator = _OPERATOR_TO_CQL2.get(condition.operator)
    if cql2_operator is None:
        return None

    value = condition.value
    if isinstance(value, bool):
        return f"{condition.field} {cql2_operator} {'true' if value else 'false'}"
    if isinstance(value, str):
        return f"{condition.field} {cql2_operator} '{_escape_cql2_string(value)}'"
    if isinstance(value, (int, float)):
        return f"{condition.field} {cql2_operator} {value}"
    return None


def _ring_to_wkt(ring: list[list[float]]) -> str:
    return "(" + ", ".join(f"{lon} {lat}" for lon, lat in ring) + ")"


def _boundary_to_wkt(boundary: dict) -> str | None:
    """Convert a GeoJSON Polygon/MultiPolygon (WGS84) to WKT for a CQL2
    S_INTERSECTS predicate."""

    geometry_type = boundary.get("type")
    coordinates = boundary.get("coordinates")
    if not coordinates:
        return None

    if geometry_type == "Polygon":
        return f"POLYGON({', '.join(_ring_to_wkt(ring) for ring in coordinates)})"
    if geometry_type == "MultiPolygon":
        polygons = ", ".join(
            "(" + ", ".join(_ring_to_wkt(ring) for ring in polygon) + ")" for polygon in coordinates
        )
        return f"MULTIPOLYGON({polygons})"

    return None


class QueryablesCache:
    """Lazily fetches and TTL-caches a collection's ``/queryables``
    schema, keyed by ``(api_url, collection)``."""

    def __init__(self, settings: GeodataSettings | None = None, http_client: httpx.Client | None = None):
        self.settings = settings or GeodataSettings()
        self._client = http_client or httpx.Client(timeout=self.settings.timeout)
        self._cache: dict[str, tuple[float, dict]] = {}

    def get(self, api_url: str, collection: str) -> dict | None:
        """Returns ``{field_name: field_schema}``, or None on fetch failure."""

        cache_key = f"{api_url}/{collection}"
        cached = self._cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < self.settings.filter_queryables_cache_ttl:
            return cached[1]

        try:
            response = self._client.get(f"{api_url}/collections/{collection}/queryables")
            response.raise_for_status()
            properties = response.json().get("properties") or {}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Failed to fetch queryables for %s/%s (%s)", api_url, collection, exc)
            return None

        self._cache[cache_key] = (time.monotonic(), properties)
        return properties

    @staticmethod
    def geometry_field(queryables: dict) -> str | None:
        for field_name, schema in queryables.items():
            if schema.get("x-ogc-role") == "primary-geometry":
                return field_name
        return None


class FilterAgent:
    """Filters an OAF layer's features via a CQL2 query, combining an
    LLM-extracted attribute condition list with an optional spatial
    predicate from an already-resolved location's boundary."""

    def __init__(
        self,
        settings: GeodataSettings | None = None,
        llm: Any = None,
        http_client: httpx.Client | None = None,
        queryables_cache: QueryablesCache | None = None,
    ):
        self.settings = settings or GeodataSettings()
        if llm is None:
            from cwym_agent.llm.qwen3_client import Qwen3Client

            llm = Qwen3Client()
        self.llm = llm
        self._client = http_client or httpx.Client(timeout=self.settings.timeout)
        self.queryables_cache = queryables_cache or QueryablesCache(self.settings, self._client)

    def filter(
        self, query: str, layer: LayerMatch | None, location: LocationMatch | None = None
    ) -> FilterResult | None:
        if layer is None or layer.typ != "OAF" or not layer.collection or not layer.url:
            if layer is not None:
                logger.info(
                    "FilterAgent: layer %r (typ=%r) is not OGC API Features-filterable, skipping",
                    layer.name,
                    layer.typ,
                )
            return None

        api_url = layer.url.rstrip("/")
        queryables = self.queryables_cache.get(api_url, layer.collection)
        conditions = self._extract_conditions(query, queryables) if queryables else []
        attribute_clauses = [
            clause for clause in (_condition_to_cql2(condition) for condition in conditions) if clause
        ]

        spatial_clause = None
        if location and location.boundary and queryables:
            geometry_field = self.queryables_cache.geometry_field(queryables)
            wkt = _boundary_to_wkt(location.boundary)
            if geometry_field and wkt:
                spatial_clause = f"S_INTERSECTS({geometry_field}, {wkt})"

        clauses = ([spatial_clause] if spatial_clause else []) + attribute_clauses
        if not clauses:
            logger.info("FilterAgent: nothing to filter %r by, skipping", layer.name)
            return None

        try:
            response = self._client.get(
                f"{api_url}/collections/{layer.collection}/items",
                params={
                    "filter": " AND ".join(clauses),
                    "filter-lang": "cql2-text",
                    "limit": self.settings.filter_result_limit,
                },
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("FilterAgent: OGC API Features query failed for %r (%s)", layer.name, exc)
            return None

        features = data.get("features") or []
        return FilterResult(
            layer_id=layer.layer_id,
            features={"type": "FeatureCollection", "features": features},
            number_matched=data.get("numberMatched"),
            label=location.name if location else None,
        )

    def _extract_conditions(self, query: str, queryables: dict) -> list[FilterCondition]:
        fields = "\n".join(
            f"- {name} ({schema.get('type', 'string')})"
            for name, schema in queryables.items()
            if schema.get("x-ogc-role") != "primary-geometry"
        )
        prompt = _CONDITION_EXTRACTION_PROMPT_TEMPLATE.format(fields=fields, query=query)

        try:
            reply = self.llm.generate(prompt, max_tokens=self.settings.filter_llm_max_tokens)
        except Exception:
            logger.warning("FilterAgent condition extraction LLM call failed", exc_info=True)
            return []

        return self._parse_conditions(reply, queryables)

    @staticmethod
    def _parse_conditions(reply: str, queryables: dict) -> list[FilterCondition]:
        match = re.search(r"\[.*\]", reply, re.DOTALL)
        if match is None:
            return []

        try:
            raw_conditions = json.loads(match.group())
        except json.JSONDecodeError:
            logger.warning("FilterAgent: could not parse condition extraction reply %r", reply)
            return []

        conditions = []
        for item in raw_conditions:
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            operator = item.get("operator")
            # Only accept fields the layer actually has and operators we
            # know how to compile - an LLM hallucinating either is a
            # dropped condition, not a malformed query sent upstream.
            if field not in queryables or operator not in _VALID_OPERATORS:
                continue
            conditions.append(FilterCondition(field=field, operator=operator, value=item.get("value")))

        return conditions
