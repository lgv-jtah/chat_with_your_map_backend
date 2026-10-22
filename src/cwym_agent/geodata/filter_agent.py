"""``FilterAgent``: translates a natural-language filter query into an
OGC API Features (OAF) CQL2 filter and executes it against a layer's
own ``/collections/{id}/items`` endpoint
(``cwym-agent-architecture.md`` Section 9).

Bypasses Masterportal's native Filter tool entirely (see the doc for
why - no OAF support server-side, predefined-query-only design, open
bugs in exactly this territory). Works against OAF layers (CQL2, below)
and WFS layers (FES 2.0, ``wfs_filter``) - both the same "attribute
conditions AND intersect with the area" filter, run on the server.

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
from shapely.geometry import shape
from shapely.prepared import prep

from cwym_agent.geodata.portal_catalog_resolver import name_key
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import FilterCondition, FilterResult, LayerMatch, LocationMatch
from cwym_agent.geodata.wfs_filter import WfsClient, boundary_to_gml, build_filter, condition_to_fes

logger = logging.getLogger(__name__)

_CONDITION_EXTRACTION_PROMPT_TEMPLATE = (
    "Du hilfst dabei, aus einer deutschen Filteranfrage strukturierte "
    "Bedingungen für eine Datenabfrage zu extrahieren. Verfügbare Felder "
    "der Kartenebene:\n{fields}\n\n"
    "Anfrage: \"{query}\"\n\n"
    "Antworte NUR mit einem JSON-Array von Bedingungen der Form "
    "{{\"field\": <Feldname>, \"operator\": <eq|ne|lt|lte|gt|gte|contains>, "
    "\"value\": <Wert>}}. Verwende NUR Felder aus der obigen Liste. Sind "
    "zu einem Feld Beispielwerte angegeben, verwende den passenden Wert "
    "genau so geschrieben. Ein Ortsname (Stadtteil, Bezirk) ist KEINE "
    "Attributbedingung - die räumliche Einschränkung erfolgt separat. Wenn "
    "die Anfrage keine konkrete Attributbedingung nennt (z.B. nur einen "
    "Ort), antworte mit einem leeren Array: []"
)

FILTER_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "contains"})
"""Public so ``build_context`` (conversation context validation, Section
10.4) can reject a client-echoed ``filterSpec`` condition whose operator
isn't one FilterAgent actually knows how to compile."""

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


def _envelope(boundary: dict) -> dict | None:
    """The bounding rectangle of an area, as a GeoJSON Polygon - what the
    service is queried with. The real outline has up to thousands of
    vertices, too big for an OAF filter in a URL; the exact check against
    it happens afterwards (``_inside``)."""

    try:
        min_x, min_y, max_x, max_y = shape(boundary).bounds
    except (ValueError, TypeError, AttributeError):
        return None
    return {
        "type": "Polygon",
        "coordinates": [[[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y], [min_x, min_y]]],
    }


def _inside(features: list[dict], boundary: dict) -> list[dict]:
    """The features that really intersect the area's outline (the service
    was only asked for its bounding rectangle). Features without a usable
    geometry are dropped."""

    try:
        area = prep(shape(boundary))
    except (ValueError, TypeError, AttributeError):
        return features
    kept = []
    for feature in features:
        try:
            if feature.get("geometry") and area.intersects(shape(feature["geometry"])):
                kept.append(feature)
        except (ValueError, TypeError, AttributeError):
            continue
    return kept


def _area_result(features: list[dict], location: LocationMatch | None, limit: int, number_matched):
    """Applies the exact area check and works out the total: exact when
    the service returned everything it matched, unknown (None) when the
    result was cut at ``limit`` - the server's own total refers to the
    rectangle, not the area."""

    if not (location and location.boundary):
        return features, number_matched
    kept = _inside(features, location.boundary)
    complete = len(features) < limit and (number_matched is None or number_matched <= len(features))
    return kept, len(kept) if complete else None


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
        wfs_client: WfsClient | None = None,
    ):
        self.settings = settings or GeodataSettings()
        if llm is None:
            from cwym_agent.llm.qwen3_client import Qwen3Client

            llm = Qwen3Client()
        self.llm = llm
        self._client = http_client or httpx.Client(timeout=self.settings.timeout)
        self.queryables_cache = queryables_cache or QueryablesCache(self.settings, self._client)
        self.wfs = wfs_client or WfsClient(self.settings, self._client)

    def filter(
        self,
        query: str,
        layer: LayerMatch | None,
        location: LocationMatch | None = None,
        mode: str = "replace",
        prior_conditions: list[FilterCondition] | None = None,
    ) -> FilterResult | None:
        """``layer`` may come from a fresh ``layer_resolver`` match, or
        (a conversation-context follow-up naming an already-active
        layer, cmym-agent-architecture.md Section 10) from
        ``LayerCatalog.get(layer_id)`` - either way it's just a
        ``LayerMatch`` here, no special-casing needed.

        ``mode="refine"`` merges this turn's extracted conditions with
        ``prior_conditions`` (Section 10.6): a new condition on a field
        that already has one replaces it ("nur Gymnasien" after "nur
        Grundschulen" means Gymnasien, not an empty intersection);
        conditions on other fields are kept (ANDed). ``mode="replace"``
        (the default) ignores ``prior_conditions`` entirely.
        """

        if layer is None:
            return None
        if layer.typ == "WFS" and layer.url and layer.feature_type:
            return self._filter_wfs(query, layer, location, mode, prior_conditions)
        if layer.typ != "OAF" or not layer.collection or not layer.url:
            logger.info(
                "FilterAgent: layer %r (typ=%r) is neither OGC API Features- nor WFS-filterable, skipping",
                layer.name,
                layer.typ,
            )
            return None

        api_url = layer.url.rstrip("/")
        queryables = self.queryables_cache.get(api_url, layer.collection)
        new_conditions = self._extract_conditions(query, queryables) if queryables else []
        conditions = self._combine(mode, prior_conditions, new_conditions)
        attribute_clauses = [
            clause for clause in (_condition_to_cql2(condition) for condition in conditions) if clause
        ]

        spatial_clause = None
        if location and location.boundary and queryables:
            geometry_field = self.queryables_cache.geometry_field(queryables)
            envelope = _envelope(location.boundary)
            wkt = _boundary_to_wkt(envelope) if envelope else None
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

        features, number_matched = _area_result(
            data.get("features") or [], location, self.settings.filter_result_limit, data.get("numberMatched")
        )
        return FilterResult(
            layer_id=layer.layer_id,
            features={"type": "FeatureCollection", "features": features},
            number_matched=number_matched,
            label=location.name if location else None,
            area_kind=location.kind if location else None,
            conditions=conditions,
        )

    def _filter_wfs(
        self,
        query: str,
        layer: LayerMatch,
        location: LocationMatch | None,
        mode: str,
        prior_conditions: list[FilterCondition] | None,
    ) -> FilterResult | None:
        """The WFS counterpart of the CQL2 path above: attribute conditions
        AND ``Intersects`` with the location's boundary, as an FES 2.0
        GetFeature request (see ``wfs_filter``)."""

        url = layer.url.split("?")[0]
        schema = self.wfs.schema(url, layer.feature_type)
        if schema is None:
            return None

        queryables = {name: {"type": xsd_type} for name, xsd_type in schema.fields.items()}
        new_conditions = self._extract_conditions(query, queryables, schema.examples)
        new_conditions += self._conditions_from_layer_name(layer, schema.examples, new_conditions)
        conditions = self._combine(mode, prior_conditions, new_conditions)
        clauses = [clause for clause in (condition_to_fes(condition) for condition in conditions) if clause]

        if location and location.boundary and schema.geometry_field:
            envelope = _envelope(location.boundary)
            gml = boundary_to_gml(envelope) if envelope else None
            if gml:
                clauses.insert(
                    0,
                    f"<fes:Intersects><fes:ValueReference>{schema.geometry_field}</fes:ValueReference>"
                    f"{gml}</fes:Intersects>",
                )

        if not clauses:
            logger.info("FilterAgent: nothing to filter %r by, skipping", layer.name)
            return None

        result = self.wfs.get_features(
            url, layer.feature_type, schema.namespace, build_filter(clauses), self.settings.filter_result_limit
        )
        if result is None:
            return None
        data, number_matched = result
        features, number_matched = _area_result(
            data.get("features") or [], location, self.settings.filter_result_limit, number_matched
        )

        return FilterResult(
            layer_id=layer.layer_id,
            features={"type": "FeatureCollection", "features": features},
            number_matched=number_matched,
            label=location.name if location else None,
            area_kind=location.kind if location else None,
            conditions=conditions,
        )

    @staticmethod
    def _conditions_from_layer_name(
        layer: LayerMatch, examples: dict[str, list[str]], extracted: list[FilterCondition]
    ) -> list[FilterCondition]:
        """A portal layer like "Gymnasien" is the schools WFS restricted to
        one category (its own wfsFilter: kapitelbezeichnung = "Gymnasien").
        If the LLM extracted no condition for the field that has the layer's
        name as one of its values, that condition is added - found live,
        the extraction otherwise sometimes returned nothing and all school
        types in the area were shown as "Gymnasien"."""

        wanted = name_key(layer.name)
        constrained = {condition.field for condition in extracted}
        for field_name, values in examples.items():
            if field_name in constrained:
                continue
            value = next((v for v in values if name_key(v) == wanted), None)
            if value is not None:
                return [FilterCondition(field=field_name, operator="eq", value=value)]
        return []

    def _combine(
        self, mode: str, prior: list[FilterCondition] | None, new: list[FilterCondition]
    ) -> list[FilterCondition]:
        return self._merge_conditions(prior, new) if mode == "refine" and prior else new

    @staticmethod
    def _merge_conditions(
        prior: list[FilterCondition], new: list[FilterCondition]
    ) -> list[FilterCondition]:
        """Section 10.6: a condition on a field the new query also
        constrains is replaced by the new one; conditions on other
        fields are kept as-is (ANDed)."""

        new_fields = {condition.field for condition in new}
        kept = [condition for condition in prior if condition.field not in new_fields]
        return kept + new

    def _extract_conditions(
        self, query: str, queryables: dict, examples: dict[str, list[str]] | None = None
    ) -> list[FilterCondition]:
        examples = examples or {}
        fields = "\n".join(
            f"- {name} ({schema.get('type', 'string')})"
            + (f" - Beispielwerte: {', '.join(examples[name])}" if examples.get(name) else "")
            for name, schema in queryables.items()
            if schema.get("x-ogc-role") != "primary-geometry"
        )
        prompt = _CONDITION_EXTRACTION_PROMPT_TEMPLATE.format(fields=fields, query=query)

        try:
            reply = self.llm.generate(
                prompt, max_tokens=self.settings.filter_llm_max_tokens, label="filter_condition_extraction"
            )
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
            if field not in queryables or operator not in FILTER_OPERATORS:
                continue
            conditions.append(FilterCondition(field=field, operator=operator, value=item.get("value")))

        return conditions
