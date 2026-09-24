"""The ``MapCommand`` protocol: a closed, typed vocabulary of executable
map actions, per ``cwym-agent-architecture.md`` Section 5.

Each command is a discriminated union member keyed on ``type``, with a
``payload`` shape specific to that type. ``CommandBuilderComponent``
constructs these; the Masterportal frontend executes each one on receipt
of the matching AG-UI ``TOOL_CALL_RESULT`` event.

``ACTIVATE_LAYER``, ``NAVIGATE_TO_LOCATION``, ``DRAW_BOUNDARY``,
``APPLY_FILTER`` (see Section 9) and ``OPEN_TOOL`` are actually built by
``CommandBuilderComponent`` today - the other four types are modeled
here for closed-enum completeness (so the frontend dispatcher has a
real schema to switch on) but nothing in the pipeline constructs them
yet: ``DEACTIVATE_LAYER``/``ZOOM_TO_EXTENT`` need resolver data that
doesn't exist yet (a "deactivate" action label, extent geometry), and
``SET_LANGUAGE``/``HIGHLIGHT_FEATURE``/``CLEAR_HIGHLIGHTS`` need intent
detection that's explicitly out of scope for this change (language-
switch detection, an explicit "clear the map" intent). This is
deliberate, not an oversight.

``APPLY_FILTER``'s payload carries the actual matched GeoJSON features
from ``FilterAgent``'s OGC API Features query as an ad-hoc overlay -
the same "agent-computed geometry pushed to the map" pattern as
``DRAW_BOUNDARY`` - not an abstract condition list for the frontend to
re-interpret against an already-loaded layer.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


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


class HighlightFeaturePayload(_CamelModel):
    layer_id: str
    feature_id: str


class ClearHighlightsPayload(_CamelModel):
    pass


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


class OpenToolPayload(_CamelModel):
    tool_name: Literal["PRINT", "LEGEND", "SHARE", "ABOUT", "CONTACT"]
    prefill: OpenToolPrefill | None = None


class SetLanguagePayload(_CamelModel):
    language_code: str


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


class HighlightFeatureCommand(_MapCommandBase):
    type: Literal["HIGHLIGHT_FEATURE"] = "HIGHLIGHT_FEATURE"
    payload: HighlightFeaturePayload


class ClearHighlightsCommand(_MapCommandBase):
    type: Literal["CLEAR_HIGHLIGHTS"] = "CLEAR_HIGHLIGHTS"
    payload: ClearHighlightsPayload = ClearHighlightsPayload()


class ApplyFilterCommand(_MapCommandBase):
    type: Literal["APPLY_FILTER"] = "APPLY_FILTER"
    payload: ApplyFilterPayload


class OpenToolCommand(_MapCommandBase):
    type: Literal["OPEN_TOOL"] = "OPEN_TOOL"
    payload: OpenToolPayload


class SetLanguageCommand(_MapCommandBase):
    type: Literal["SET_LANGUAGE"] = "SET_LANGUAGE"
    payload: SetLanguagePayload


MapCommand = Annotated[
    Union[
        ActivateLayerCommand,
        DeactivateLayerCommand,
        NavigateToLocationCommand,
        ZoomToExtentCommand,
        DrawBoundaryCommand,
        HighlightFeatureCommand,
        ClearHighlightsCommand,
        ApplyFilterCommand,
        OpenToolCommand,
        SetLanguageCommand,
    ],
    Field(discriminator="type"),
]
