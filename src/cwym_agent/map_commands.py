"""The ``MapCommand`` protocol: a closed, typed vocabulary of executable
map actions, per ``cwym-agent-architecture.md`` Section 5.

Each command is a discriminated union member keyed on ``type``, with a
``payload`` shape specific to that type. ``CommandBuilderComponent``
constructs these; the Masterportal frontend executes each one on receipt
of the matching AG-UI ``TOOL_CALL_RESULT`` event.

``ACTIVATE_LAYER``, ``DEACTIVATE_LAYER``, ``NAVIGATE_TO_LOCATION``,
``ZOOM_TO_EXTENT``, ``DRAW_BOUNDARY``, ``APPLY_FILTER`` (see Section 9),
``CLEAR_FILTER``, ``ADD_RESULT_LAYER``, ``CLEAR_RESULT_LAYERS`` and
``OPEN_TOOL`` are actually built by
``CommandBuilderComponent`` today - the other types are modeled here
for closed-enum completeness (so the frontend dispatcher has a real
schema to switch on) but nothing in the pipeline constructs them yet:
``SET_LANGUAGE``/``CLEAR_HIGHLIGHTS`` need intent
detection that's explicitly out of scope for this change (language-
switch detection, an explicit "clear the map" intent). This is
deliberate, not an oversight.

``ZOOM_TO_EXTENT`` fires alongside ``NAVIGATE_TO_LOCATION`` whenever a
resolved location has a sensible area to fit the view to
(``LocationMatch.extent`` - derived from a real address-search
boundary, or the profile's extent for the city itself, which has none)
- found live: without it, navigating to the city itself left the map zoomed in
on the city center at a zoom level tuned for a single address, not a
whole city.

``APPLY_FILTER``'s payload carries the actual matched GeoJSON features
from ``FilterAgent``'s OGC API Features query as an ad-hoc overlay -
the same "agent-computed geometry pushed to the map" pattern as
``DRAW_BOUNDARY`` - not an abstract condition list for the frontend to
re-interpret against an already-loaded layer.

``CLEAR_FILTER`` undoes one: it drops that layer's overlay and its
recorded filterSpec on the frontend ("zeig wieder alle"). A dedicated
command rather than a DEACTIVATE_LAYER convention, since the overlay
isn't a catalog layer and the frontend's filter state has to be cleared
along with it. It never restores the full layer by itself -
``CommandBuilderComponent`` pairs it with an ``ACTIVATE_LAYER``.

``ADD_RESULT_LAYER`` carries one layer of a UDP agent analysis (Section
13) as another ad-hoc GeoJSON overlay. Unlike the single filter overlay,
an analysis usually has several (the result plus the area it was
restricted to), so each is keyed by ``layerKey``, and a new analysis
starts with ``CLEAR_RESULT_LAYERS`` to remove the previous one's.
"""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from cwym_agent.city_profile import LocationKind


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# -- shared payload fragments ------------------------------------------


class Coordinates(_CamelModel):
    """WGS84 (EPSG:4326). The frontend transforms to the map's internal
    EPSG:25832 on receipt."""

    lon: float
    lat: float


class NamedLocation(_CamelModel):
    name: str
    coordinates: Coordinates
    kind: LocationKind | None = None
    """Lets the frontend record `lastLocation` (Section 10.3) for a
    single-location NAVIGATE_TO_LOCATION without guessing - absent for
    a resolved hit with no kind (e.g. a street-level match)."""


class OpenToolPrefill(_CamelModel):
    subject: str | None = None
    message: str | None = None


# -- per-type payloads ----------------------------------------------------


class ActivateLayerPayload(_CamelModel):
    layer_ids: list[str] = Field(min_length=1)


class DeactivateLayerPayload(_CamelModel):
    layer_ids: list[str] = Field(min_length=1)


class NavigateToLocationPayload(_CamelModel):
    locations: list[NamedLocation] = Field(min_length=1)
    show_markers: bool = True
    zoom: int | None = None


class ZoomToExtentPayload(_CamelModel):
    bbox: tuple[float, float, float, float]
    padding_ratio: float = 0.1


class DrawBoundaryPayload(_CamelModel):
    geometry: dict
    style: Literal["highlight", "outline"] = "outline"
    label: str | None = None
    kind: LocationKind | None = None
    """Lets the frontend record `lastLocation` (Section 10.3) alongside
    `label` without guessing."""


class HighlightFeaturesPayload(_CamelModel):
    """Features to emphasise on the map (a tier 1 result, CHAT-14) - drawn
    in their own highlight layer, which the next HIGHLIGHT_FEATURES or a
    CLEAR_HIGHLIGHTS replaces."""

    features: dict
    """A GeoJSON FeatureCollection (WGS84)."""
    style: Literal["query", "selection"] = "query"
    """"query": the answer to a question; "selection": what the user
    picked on the map (CHAT-16)."""
    label: str | None = None
    total_matched: int | None = None
    """How many matched in total, if more than ``features``."""


class ClearHighlightsPayload(_CamelModel):
    pass


class FilterConditionPayload(_CamelModel):
    field: str
    operator: str
    value: Any


class FilterAreaPayload(_CamelModel):
    name: str
    kind: LocationKind


class FilterSpecPayload(_CamelModel):
    """Descriptive only - the frontend stores this with the overlay and
    echoes it back in a later request's state.filters, but never
    interprets it (cwym-agent-architecture.md Section 10.6)."""

    conditions: list[FilterConditionPayload]
    area: FilterAreaPayload | None = None


class ApplyFilterPayload(_CamelModel):
    layer_id: str
    features: dict
    """A GeoJSON FeatureCollection - the matched features from
    FilterAgent's OGC API Features query (Section 9), pushed as an
    ad-hoc overlay."""
    label: str | None = None
    total_matched: int | None = None
    """numberMatched from the OGC API Features response, if the server
    reported it (optional per spec)."""
    truncated: bool = False
    """True when total_matched is known and exceeds len(features) -
    FilterAgent caps how many features it requests per query."""
    filter_spec: FilterSpecPayload | None = None
    """The validated, merged conditions + area that produced these
    features - lets a later refinement ("und jetzt die Gymnasien")
    merge against what's actually active, without the backend needing
    to remember anything server-side."""


class ClearFilterPayload(_CamelModel):
    layer_id: str
    """The layer whose APPLY_FILTER overlay (and recorded filterSpec)
    the frontend should drop - the same id APPLY_FILTER carried."""


class AddResultLayerPayload(_CamelModel):
    layer_key: str
    label: str
    role: Literal["result", "context"]
    """"result": the features that answer the question, drawn prominently.
    "context": reference geometry (e.g. the district the analysis was
    restricted to), drawn as an outline underneath."""
    features: dict
    """A GeoJSON FeatureCollection (WGS84)."""
    feature_count: int
    total_features: int
    truncated: bool = False
    geometry_type: str | None = None


class ClearResultLayersPayload(_CamelModel):
    pass


class OpenToolPayload(_CamelModel):
    tool_name: Literal["PRINT", "LEGEND", "SHARE", "ABOUT", "CONTACT"]
    prefill: OpenToolPrefill | None = None


class SetLanguagePayload(_CamelModel):
    language_code: str


HexColor = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=120)]


class ChartPoint(_CamelModel):
    label: str = Field(min_length=1, max_length=100)
    value: float
    color: HexColor | None = None
    kind: Literal["value", "other", "missing"] = "value"
    """"other": the "Sonstige" bar of a top-K distribution; "missing":
    features without a value - lets the UI style them apart."""


class ShowChartPayload(_CamelModel):
    """A chart next to the answer (CHAT-14). The answer text carries the
    same numbers - the chart is never the only place they appear."""

    title: str = Field(min_length=1, max_length=200)
    chart_type: Literal["bar"] = "bar"
    data: list[ChartPoint] = Field(min_length=1, max_length=50)
    total: int | None = None
    """The number of features the chart is about (for percentages)."""
    dataset_key: str | None = None
    attribute: str | None = None


class ShowSuggestionsPayload(_CamelModel):
    """Follow-up questions to offer as buttons; clicking one sends it as
    the next user message. In the answer's language."""

    items: list[ShortText] = Field(min_length=1, max_length=3)


class TourStep(_CamelModel):
    delay_ms: int = Field(ge=0, le=120_000)
    """Wait before this step, after the previous one's narration."""
    commands: list["MapCommand"] = Field(default_factory=list)
    narration: str = Field(min_length=1)

    @model_validator(mode="after")
    def _no_nested_tours(self) -> "TourStep":
        if any(command.type == "START_TOUR" for command in self.commands):
            raise ValueError("a tour step can't start another tour")
        return self


class StartTourPayload(_CamelModel):
    """A guided tour (CHAT-17): the addon plays the steps' commands with the
    normal executor and types out the narration."""

    tour_id: str = Field(min_length=1)
    steps: list[TourStep] = Field(min_length=1, max_length=50)
    follow_up: str | None = None
    suggestions: list[ShortText] = Field(default_factory=list, max_length=3)


# -- per-type commands ------------------------------------------------------


class _MapCommandBase(_CamelModel):
    command_id: str


class ActivateLayerCommand(_MapCommandBase):
    type: Literal["ACTIVATE_LAYER"] = "ACTIVATE_LAYER"
    payload: ActivateLayerPayload


class DeactivateLayerCommand(_MapCommandBase):
    type: Literal["DEACTIVATE_LAYER"] = "DEACTIVATE_LAYER"
    payload: DeactivateLayerPayload


class NavigateToLocationCommand(_MapCommandBase):
    type: Literal["NAVIGATE_TO_LOCATION"] = "NAVIGATE_TO_LOCATION"
    payload: NavigateToLocationPayload


class ZoomToExtentCommand(_MapCommandBase):
    type: Literal["ZOOM_TO_EXTENT"] = "ZOOM_TO_EXTENT"
    payload: ZoomToExtentPayload


class DrawBoundaryCommand(_MapCommandBase):
    type: Literal["DRAW_BOUNDARY"] = "DRAW_BOUNDARY"
    payload: DrawBoundaryPayload


class HighlightFeaturesCommand(_MapCommandBase):
    type: Literal["HIGHLIGHT_FEATURES"] = "HIGHLIGHT_FEATURES"
    payload: HighlightFeaturesPayload


class ClearHighlightsCommand(_MapCommandBase):
    type: Literal["CLEAR_HIGHLIGHTS"] = "CLEAR_HIGHLIGHTS"
    payload: ClearHighlightsPayload = ClearHighlightsPayload()


class ApplyFilterCommand(_MapCommandBase):
    type: Literal["APPLY_FILTER"] = "APPLY_FILTER"
    payload: ApplyFilterPayload


class ClearFilterCommand(_MapCommandBase):
    type: Literal["CLEAR_FILTER"] = "CLEAR_FILTER"
    payload: ClearFilterPayload


class AddResultLayerCommand(_MapCommandBase):
    type: Literal["ADD_RESULT_LAYER"] = "ADD_RESULT_LAYER"
    payload: AddResultLayerPayload


class ClearResultLayersCommand(_MapCommandBase):
    type: Literal["CLEAR_RESULT_LAYERS"] = "CLEAR_RESULT_LAYERS"
    payload: ClearResultLayersPayload = ClearResultLayersPayload()


class OpenToolCommand(_MapCommandBase):
    type: Literal["OPEN_TOOL"] = "OPEN_TOOL"
    payload: OpenToolPayload


class SetLanguageCommand(_MapCommandBase):
    type: Literal["SET_LANGUAGE"] = "SET_LANGUAGE"
    payload: SetLanguagePayload


class ShowChartCommand(_MapCommandBase):
    type: Literal["SHOW_CHART"] = "SHOW_CHART"
    payload: ShowChartPayload


class ShowSuggestionsCommand(_MapCommandBase):
    type: Literal["SHOW_SUGGESTIONS"] = "SHOW_SUGGESTIONS"
    payload: ShowSuggestionsPayload


class StartTourCommand(_MapCommandBase):
    type: Literal["START_TOUR"] = "START_TOUR"
    payload: StartTourPayload


_COMMANDS = (
    ActivateLayerCommand,
    DeactivateLayerCommand,
    NavigateToLocationCommand,
    ZoomToExtentCommand,
    DrawBoundaryCommand,
    HighlightFeaturesCommand,
    ClearHighlightsCommand,
    ApplyFilterCommand,
    ClearFilterCommand,
    AddResultLayerCommand,
    ClearResultLayersCommand,
    OpenToolCommand,
    SetLanguageCommand,
    ShowChartCommand,
    ShowSuggestionsCommand,
    StartTourCommand,
)

MapCommand = Annotated[Union[_COMMANDS], Field(discriminator="type")]

TourStep.model_rebuild()
StartTourPayload.model_rebuild()
StartTourCommand.model_rebuild()

COMMAND_TYPES: tuple[str, ...] = tuple(command.model_fields["type"].default for command in _COMMANDS)
"""Every MapCommand type, in declaration order."""

LEGACY_COMMAND_TYPES: frozenset[str] = frozenset(
    {
        "ACTIVATE_LAYER", "DEACTIVATE_LAYER", "NAVIGATE_TO_LOCATION", "ZOOM_TO_EXTENT", "DRAW_BOUNDARY",
        "APPLY_FILTER", "CLEAR_FILTER", "ADD_RESULT_LAYER", "CLEAR_RESULT_LAYERS", "OPEN_TOOL",
        "CLEAR_HIGHLIGHTS",
    }
)
"""What a client that doesn't send ``supported_commands`` executes - the
addon before CHAT-15. The backend only emits other types to clients that
declare them."""


def supported_command_types(declared) -> frozenset[str]:
    """The command types to emit for a client: the known types it declares
    (``forwardedProps.supported_commands``), always including the legacy
    ones; the legacy set when it declares nothing usable."""

    if not isinstance(declared, list):
        return LEGACY_COMMAND_TYPES
    return LEGACY_COMMAND_TYPES | {t for t in declared if isinstance(t, str) and t in COMMAND_TYPES}
