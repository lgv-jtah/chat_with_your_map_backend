"""Shared result types for location/layer/action resolution.

Used by both ``cwym_agent.stubs.data_services.StubDataServices`` and
``cwym_agent.geodata.real_data_services.RealDataServices``, so both can
be swapped in behind the same ``AgentExecutorComponent`` without it
needing to know which implementation is in use.
"""

from dataclasses import dataclass, field


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
    extent: tuple[float, float, float, float] | None = None
    """WGS84 bounding box (minLon, minLat, maxLon, maxLat) to fit the map
    view to (ZOOM_TO_EXTENT), distinct from `boundary` (which drives
    DRAW_BOUNDARY's outline) - set whenever there's a sensible area to
    zoom to: derived from `boundary` when one exists, or the city
    profile's extent for the city itself, which has no boundary geometry
    to derive one from."""
    kind: str | None = None
    """One of the city profile's location kinds (the values of
    ``geocoder.place_types``, e.g. "stadtteil"), or None when the resolved
    hit is street-level or otherwise none of them. Lets a location travel
    by name+kind for later re-resolution (conversation context,
    cwym-agent-architecture.md Section 10) without trusting client-
    supplied geometry. The geocoder adapter maps its service's own place
    types to these kinds."""
    alternatives: list[str] = field(default_factory=list)
    """Other areas the query could equally mean, e.g. "Hoheluft" ->
    this match is "Hoheluft-Ost", alternatives ["Hoheluft-West"]. Non-empty
    means the place is ambiguous: no map commands are built and the answer
    asks the user which one they mean."""


def bbox_of(geometry: dict) -> tuple[float, float, float, float]:
    """WGS84 (minLon, minLat, maxLon, maxLat) covering a GeoJSON
    Polygon/MultiPolygon - for ZOOM_TO_EXTENT, alongside the geometry
    itself (which DRAW_BOUNDARY outlines directly)."""

    def points(coords):
        if isinstance(coords[0], (int, float)):
            yield coords
            return
        for item in coords:
            yield from points(item)

    lons, lats = zip(*points(geometry["coordinates"]))
    return min(lons), min(lats), max(lons), max(lats)


@dataclass
class LayerMatch:
    name: str
    layer_id: str
    confidence: float
    description: str = ""
    typ: str | None = None
    """The service type the layer catalog reports (e.g. "OAF", "WMS",
    "WFS", "SensorThings") - FilterAgent only operates on "OAF" layers
    (see cwym-agent-architecture.md Section 9)."""
    url: str | None = None
    """The service's API root, e.g. "https://example.org/datasets/v1/
    stadtrad" for an OAF layer - FilterAgent builds
    f"{url}/collections/{collection}/items" from this."""
    collection: str | None = None
    """The OGC API Features collection id (only set for typ == "OAF"),
    e.g. "stadtrad_stationen" - distinct from layer_id, which is the
    layer catalog's own id."""
    feature_type: str | None = None
    """The WFS feature type name without prefix (only for typ == "WFS"),
    e.g. "staatliche_schulen" - services.json's ``featureType``.
    FilterAgent needs it (plus ``feature_ns``) to query a WFS layer."""
    feature_ns: str | None = None
    """The WFS feature type's namespace URI, e.g.
    "http://www.deegree.org/de.hh.up" - services.json's ``featureNS``."""


@dataclass
class ActionMatch:
    action_type: str
    confidence: float
    deactivate_layer_ids: list[str] = field(default_factory=list)
    """Active layer ids the query explicitly asked to stop showing
    (e.g. "zeige U-Bahn, aber nicht mehr Fahrradstationen") - matched
    by the planner itself against the "Aktive Ebenen" it already sees
    in its own prompt (Section 10.5), passed straight through as plan
    args rather than re-resolved here. Empty for an ordinary Zeigen
    turn with no such instruction - a new layer only ever *adds* to
    what's active unless the query says otherwise."""
    clear_filter_layer_ids: list[str] = field(default_factory=list)
    """Layer ids whose active filter the query explicitly asked to drop
    ("zeig wieder alle", "Filter entfernen") - matched by the planner
    against the "Aktive Filter" in its prompt, same pass-through as
    deactivate_layer_ids."""


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
    number_matched: int | None
    """OGC API Features' numberMatched, if the server reported it - it's
    optional per spec, so None here means "unknown", distinct from
    "matched exactly what was returned"."""
    label: str | None = None
    area_kind: str | None = None
    """The spatially-filtering location's `kind` ("bezirk"/"stadtteil"/
    "address"), when one was used - together with `label`, echoed back
    to the frontend as ApplyFilterPayload.filterSpec.area so a
    follow-up refinement can re-resolve the same area by name+kind
    (cmym-agent-architecture.md Section 10.6)."""
    conditions: list[FilterCondition] = field(default_factory=list)
    """The validated attribute conditions actually compiled into the
    query (post-merge on a refine turn) - echoed back as
    ApplyFilterPayload.filterSpec.conditions so a later refinement has
    something to merge against."""


@dataclass
class ResultLayer:
    """One map layer the UDP agent published for an analysis
    (cmym-agent-architecture.md Section 13): a GeoJSON FeatureCollection
    in WGS84 plus what it shows."""

    key: str
    label: str
    role: str
    """"result" (the features that answer the question) or "context"
    (reference geometry such as the district the analysis was
    restricted to)."""
    features: dict
    feature_count: int
    total_features: int
    truncated: bool = False
    geometry_type: str | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class AnalysisResult:
    """The UDP agent's answer to an analytical question, ready for
    TextResponseComponent (answer, streamed verbatim) and
    CommandBuilderComponent (layers)."""

    answer: str | None
    layers: list[ResultLayer] = field(default_factory=list)
    error: str | None = None
    """Set when the UDP agent couldn't be reached or failed - answer is
    then None."""
