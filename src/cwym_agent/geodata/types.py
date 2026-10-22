"""Shared result types for location/layer/action resolution.

Used by both ``cwym_agent.stubs.data_services.StubDataServices`` and
``cwym_agent.geodata.real_data_services.RealDataServices``, so both can
be swapped in behind the same ``AgentExecutorComponent`` without it
needing to know which implementation is in use.
"""

from dataclasses import dataclass, field

# HAMBURG_EXTENT is chat_with_your_map_portal/config.json's own
# portalConfig.map.mapView.extent - [510000.0, 5850000.0, 625000.4,
# 6000000.0] in EPSG:25832 - converted to WGS84 via pyproj
# (Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)),
# not a from-scratch guess: reuses the portal author's own already-
# vetted "sensible whole-region view", not a data-derived union of
# Hamburg's boroughs (which would technically be more "correct" but
# includes far-flung exclaves like Neuwerk, an island in the North Sea
# that's administratively part of Hamburg-Mitte - zooming out that far
# would make the actual urban area look tiny, the opposite of what a
# "show me Hamburg" default should look like).
HAMBURG_EXTENT = (9.148322816744086, 52.79970456940332, 10.913170524761878, 54.13290152266379)


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
    zoom to: derived from `boundary` when one exists, or a hardcoded
    region extent for a location (like the Hamburg-wide fallback) that
    has no real boundary geometry to derive one from."""
    kind: str | None = None
    """"bezirk" | "stadtteil" | "address", or None when the resolved hit
    is street-level or otherwise doesn't fit one of those (matches the
    MapCommand schema's Area/NamedLocation `kind` enum). Lets a location
    travel by name+kind for later re-resolution (conversation context,
    cwym-agent-architecture.md Section 10) without trusting client-
    supplied geometry. Derived from Hamburg's own address index `type`
    field - verified live (ImplementationLog.md): `"county"` is Bezirk
    (e.g. "Altona", "Hamburg-Harburg"), `"district"` is Stadtteil (e.g.
    "Altona-Altstadt", "Ottensen", "Eppendorf" - including a Stadtteil
    that shares its Bezirk's exact name, like "Eimsbüttel"), `"address"`
    is address. `"state"`/`"city"` (also in `_BROAD_TYPES`) have no
    observed hits in Hamburg's index and no clean equivalent here, so
    they map to None rather than a guessed value."""
    alternatives: list[str] = field(default_factory=list)
    """Other areas the query could equally mean, e.g. "Hoheluft" ->
    this match is "Hoheluft-Ost", alternatives ["Hoheluft-West"]. Non-empty
    means the place is ambiguous: no map commands are built and the answer
    asks the user which one they mean."""


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
