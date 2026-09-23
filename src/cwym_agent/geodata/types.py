"""Shared result types for location/layer/action resolution.

Used by both ``cwym_agent.stubs.data_services.StubDataServices`` and
``cwym_agent.geodata.real_data_services.RealDataServices``, so both can
be swapped in behind the same ``AgentExecutorComponent`` without it
needing to know which implementation is in use.
"""

from dataclasses import dataclass


@dataclass
class LocationMatch:
    name: str
    latitude: float
    longitude: float
    confidence: float
    boundary: dict | None = None
    """GeoJSON Polygon/MultiPolygon (WGS84), or None when the resolved hit
    has no area geometry (street/address-level matches, or the source
    endpoint's area field was itself a Point rather than a polygon)."""


@dataclass
class LayerMatch:
    name: str
    layer_id: str
    confidence: float
    description: str = ""
    typ: str | None = None
    """The service type Hamburg's catalog reports (e.g. "OAF", "WMS",
    "WFS", "SensorThings") - FilterAgent only operates on "OAF" layers
    (see cwym-agent-architecture.md Section 9)."""
    url: str | None = None
    """The service's API root, e.g. "https://api.hamburg.de/datasets/v1/
    stadtrad" for an OAF layer - FilterAgent builds
    f"{url}/collections/{collection}/items" from this."""
    collection: str | None = None
    """The OGC API Features collection id (only set for typ == "OAF"),
    e.g. "stadtrad_stationen" - distinct from layer_id, which is
    Hamburg's own catalog id."""


@dataclass
class ActionMatch:
    action_type: str
    confidence: float


@dataclass
class FilterCondition:
    """An attribute condition FilterAgent extracted from a natural-
    language filter query (internal representation, compiled to a CQL2
    clause - not a wire type; see cwym_agent.geodata.filter_agent)."""

    field: str
    operator: str
    value: object


@dataclass
class FilterResult:
    """FilterAgent's result: the matched features from an OGC API
    Features query, ready for CommandBuilderComponent to push as an
    APPLY_FILTER MapCommand (cwym-agent-architecture.md Section 9)."""

    layer_id: str
    features: dict
    """A GeoJSON FeatureCollection."""
    number_matched: int
    label: str | None = None
