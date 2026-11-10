"""Tier 1 analysis: getting *all* the features a question is about
(CHAT-11, cmym-agent-architecture.md 14.4, rule 1).

``FilterAgent`` shows at most ``CWYM_FILTER_RESULT_LIMIT`` features; an
aggregate over that would be silently wrong. Here every source returns
either the complete set (or, for a plain count, the server's exact
total) or says it can't (``FeatureSet.complete`` False) - never a part
passed off as the whole.

- Counts without an area use the server's total: OGC API Features
  ``numberMatched``, WFS ``resultType=hits``.
- Everything else pages through the matches (OAF ``next`` links, WFS
  ``startIndex``) up to ``CWYM_ANALYSIS_MAX_FEATURES``.
- An area is asked for as its bounding rectangle (as ``FilterAgent``
  does) and checked against the real outline here.
- A condition goes to the server only if the server compares it the way
  the declared type means it: equality and "contains" always, ranges
  only on numeric server fields. Ranges on ordinals ("Energieklasse
  schlechter als E") and on numbers stored as text are applied here.
- A ``file`` dataset is loaded once at startup and filtered here.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from cwym_agent.city_profile import AttributeConfig
from cwym_agent.geodata.analysis import parse_number
from cwym_agent.geodata.filter_agent import _boundary_to_wkt, _condition_to_cql2, _envelope, _inside
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import FilterCondition
from cwym_agent.geodata.wfs_filter import WfsClient, WfsSchema, boundary_to_gml, build_filter, condition_to_fes

logger = logging.getLogger(__name__)

_RANGE_OPERATORS = frozenset({"lt", "lte", "gt", "gte"})
_NUMERIC_SERVER_TYPES = frozenset(
    {"integer", "number", "int", "long", "short", "double", "float", "decimal", "byte",
     "nonnegativeinteger", "positiveinteger", "unsignedint", "unsignedlong"}
)


class DatasetError(RuntimeError):
    """A dataset file can't be used. Raised at startup."""


@dataclass
class FeatureSet:
    features: list[dict] | None
    """Every matching feature - None when only the total was asked for."""
    total: int | None
    """Exact number of matches; None if not known."""
    complete: bool
    """False: the matches couldn't be fetched completely - don't compute."""
    reason: str | None = None
    """Why incomplete: "too_many" (above the cap) or "service_error"."""


class AnalysisSource(Protocol):
    def fetch(
        self, conditions: Sequence[FilterCondition], boundary: dict | None, need_features: bool
    ) -> FeatureSet: ...


# --- conditions evaluated here ------------------------------------------------


def _ordinal_rank(attribute: AttributeConfig, value) -> int | None:
    order = attribute.order or []
    return order.index(str(value).strip()) if str(value).strip() in order else None


def _compare(left, right, operator: str) -> bool:
    return {
        "eq": left == right, "ne": left != right, "lt": left < right,
        "lte": left <= right, "gt": left > right, "gte": left >= right,
    }[operator]


def condition_matches(properties: dict, condition: FilterCondition, attribute: AttributeConfig | None) -> bool:
    """Whether a feature meets a condition, by the attribute's declared type.
    A missing value never matches (like SQL NULL)."""

    raw = properties.get(condition.field)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False
    operator, wanted = condition.operator, condition.value
    if isinstance(wanted, list):
        if operator == "eq":
            return any(condition_matches(properties, FilterCondition(condition.field, "eq", w), attribute) for w in wanted)
        return False
    if operator == "contains":
        return str(wanted).casefold() in str(raw).casefold()

    if attribute is not None and attribute.type == "ordinal":
        left, right = _ordinal_rank(attribute, raw), _ordinal_rank(attribute, wanted)
        if left is not None and right is not None:
            return _compare(left, right, operator)
    numbers = parse_number(raw), parse_number(wanted)
    if (attribute is None or attribute.type == "number") and None not in numbers:
        return _compare(numbers[0], numbers[1], operator)
    if operator in ("eq", "ne"):
        return _compare(str(raw).strip().casefold(), str(wanted).strip().casefold(), operator)
    return False  # a range on values that don't order


def _server_can(condition: FilterCondition, attribute: AttributeConfig | None, server_type: str | None) -> bool:
    """Whether the server compares ``condition`` the way the declared type
    means it."""

    if isinstance(condition.value, list):
        return False
    if condition.operator not in _RANGE_OPERATORS:
        return True
    if attribute is not None and attribute.type == "ordinal":
        return False
    return (server_type or "").lower() in _NUMERIC_SERVER_TYPES and parse_number(condition.value) is not None


def _local_filter(features: list[dict], conditions, attributes: dict[str, AttributeConfig], boundary: dict | None):
    kept = [
        f for f in features
        if all(condition_matches(f.get("properties") or {}, c, attributes.get(c.field)) for c in conditions)
    ]
    return _inside(kept, boundary) if boundary else kept


# --- file ------------------------------------------------------------------------


def load_feature_collection(path: Path) -> list[dict]:
    """The features of a GeoJSON FeatureCollection file (WGS84)."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"dataset file {path} can't be read as JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise DatasetError(f"dataset file {path} is not a GeoJSON FeatureCollection")
    features = [f for f in data["features"] if isinstance(f, dict) and isinstance(f.get("properties"), dict)]
    if len(features) != len(data["features"]):
        raise DatasetError(f"dataset file {path}: {len(data['features']) - len(features)} features have no properties")
    return features


class FileSource:
    def __init__(self, features: list[dict], attributes: dict[str, AttributeConfig]):
        self.features = features
        self.attributes = attributes

    def fetch(self, conditions, boundary, need_features) -> FeatureSet:
        kept = _local_filter(self.features, conditions, self.attributes, boundary)
        return FeatureSet(kept, len(kept), True)


# --- OGC API Features ----------------------------------------------------------


class OafSource:
    def __init__(
        self,
        url: str,
        collection: str,
        queryables: dict,
        geometry_field: str | None,
        attributes: dict[str, AttributeConfig],
        http_client: httpx.Client,
        settings: GeodataSettings,
    ):
        self.items_url = f"{url.rstrip('/')}/collections/{collection}/items"
        self.queryables = queryables
        self.geometry_field = geometry_field
        self.attributes = attributes
        self._client = http_client
        self.settings = settings

    def fetch(self, conditions, boundary, need_features) -> FeatureSet:
        server, local = [], []
        for c in conditions:
            server_type = (self.queryables.get(c.field) or {}).get("type")
            compiled = _condition_to_cql2(c) if _server_can(c, self.attributes.get(c.field), server_type) else None
            (server if compiled else local).append(compiled or c)
        if boundary is not None:
            envelope = _envelope(boundary)
            wkt = _boundary_to_wkt(envelope) if envelope else None
            if not (self.geometry_field and wkt):
                return FeatureSet(None, None, False, "service_error")
            server.insert(0, f"S_INTERSECTS({self.geometry_field}, {wkt})")
        params = {"filter": " AND ".join(server), "filter-lang": "cql2-text"} if server else {}

        if not need_features and not local and boundary is None:
            total = self._number_matched(params)
            if total is not None:
                return FeatureSet(None, total, True)

        features, failure = self._all_pages(params)
        if failure:
            return FeatureSet(None, None, False, failure)
        kept = _local_filter(features, local, self.attributes, boundary)
        return FeatureSet(kept, len(kept), True)

    def _get(self, url: str, params: dict | None) -> dict | None:
        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("analysis: OGC API Features request failed (%s)", exc)
            return None

    def _number_matched(self, params: dict) -> int | None:
        data = self._get(self.items_url, {**params, "limit": 1})
        value = (data or {}).get("numberMatched")
        return value if isinstance(value, int) else None

    def _all_pages(self, params: dict) -> tuple[list[dict], str | None]:
        page_size, cap = self.settings.analysis_page_size, self.settings.analysis_max_features
        url, query = self.items_url, {**params, "limit": page_size}
        features: list[dict] = []
        offset = 0
        while True:
            data = self._get(url, query)
            if data is None:
                return [], "service_error"
            if not features and isinstance(data.get("numberMatched"), int) and data["numberMatched"] > cap:
                return [], "too_many"
            page = data.get("features") or []
            features += page
            if len(features) > cap:
                return [], "too_many"
            next_link = next(
                (link.get("href") for link in data.get("links") or [] if link.get("rel") == "next" and link.get("href")),
                None,
            )
            if not page or (len(page) < page_size and next_link is None):
                return features, None
            if next_link:
                url, query = next_link, None
            else:
                offset += len(page)
                url, query = self.items_url, {**params, "limit": page_size, "offset": offset}


# --- WFS --------------------------------------------------------------------------


class WfsSource:
    def __init__(
        self,
        url: str,
        feature_type: str,
        schema: WfsSchema,
        attributes: dict[str, AttributeConfig],
        wfs: WfsClient,
        settings: GeodataSettings,
    ):
        self.url = url.split("?")[0]
        self.feature_type = feature_type
        self.schema = schema
        self.attributes = attributes
        self.wfs = wfs
        self.settings = settings

    def fetch(self, conditions, boundary, need_features) -> FeatureSet:
        server, local = [], []
        for c in conditions:
            server_type = self.schema.fields.get(c.field)
            compiled = condition_to_fes(c) if _server_can(c, self.attributes.get(c.field), server_type) else None
            (server if compiled else local).append(compiled or c)
        if boundary is not None:
            envelope = _envelope(boundary)
            gml = boundary_to_gml(envelope) if envelope else None
            if not (self.schema.geometry_field and gml):
                return FeatureSet(None, None, False, "service_error")
            server.insert(
                0,
                f"<fes:Intersects><fes:ValueReference>{self.schema.geometry_field}</fes:ValueReference>{gml}</fes:Intersects>",
            )
        filter_xml = build_filter(server)
        args = (self.url, self.feature_type, self.schema.namespace, filter_xml)

        total = self.wfs.count(*args)
        if not need_features and not local and boundary is None and total is not None:
            return FeatureSet(None, total, True)
        cap = self.settings.analysis_max_features
        if total is not None and total > cap:
            return FeatureSet(None, None, False, "too_many")

        page_size = self.settings.analysis_page_size
        features: list[dict] = []
        while True:
            data = self.wfs.get_page(*args, page_size, len(features))
            if data is None:
                return FeatureSet(None, None, False, "service_error")
            page = data.get("features") or []
            features += page
            if len(features) > cap:
                return FeatureSet(None, None, False, "too_many")
            if len(page) < page_size or (total is not None and len(features) >= total):
                break
        kept = _local_filter(features, local, self.attributes, boundary)
        return FeatureSet(kept, len(kept), True)
