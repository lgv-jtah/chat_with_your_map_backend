"""``CommandBuilderComponent``: turns aggregated sub-agent results into an
ordered list of typed ``MapCommand``s (see ``cwym_agent.map_commands``),
and streams each one individually as its own AG-UI ``TOOL_CALL_*`` cycle
via the bridge, so the frontend can execute ``ACTIVATE_LAYER`` while
``NAVIGATE_TO_LOCATION`` is still being built, instead of waiting for a
single combined payload.

When a resolved location carries a district-level area boundary, the
build order for a "Zeigen"/"Navigieren" turn is ``ACTIVATE_LAYER ->
DRAW_BOUNDARY -> NAVIGATE_TO_LOCATION -> ZOOM_TO_EXTENT`` - draws the
district for visual context, but (unlike an earlier version of this
component) does not spatially restrict the activated layer's own
features; that's now ``FilterAgent``'s job for a "Filter" turn, via
real server-side OGC API Features CQL2 queries rather than client-side
masking (see ``cwym-agent-architecture.md`` Section 9).

A "Filter" turn's own build order is ``[DEACTIVATE_LAYER] ->
APPLY_FILTER -> [ZOOM_TO_EXTENT]``. The trailing ``ZOOM_TO_EXTENT`` -
found live to be missing entirely: a spatially-scoped filter (e.g.
"Fahrradstationen in Wandsbek") pushed matched features onto the map
with no view change at all, so they could land anywhere relative to
whatever was already on screen - reuses the resolved location's
``extent`` when one exists, same field the Zeigen/Navigieren path
already uses.

``ZOOM_TO_EXTENT`` comes *after* ``NAVIGATE_TO_LOCATION`` deliberately:
the frontend executes each command as it arrives with no batching
(Section 3), and ``NAVIGATE_TO_LOCATION``'s own client-side zoom
default is tuned for a single point/address, not an area the size of a
district or a whole city - found live: navigating to "Hamburg" with no
explicit zoom left the map zoomed in on the city center, needing a
manual zoom-out to see the city. ``ZOOM_TO_EXTENT`` fits the view to
``LocationMatch.extent`` right after, overriding that default with a
properly city/district-scaled view, while ``NAVIGATE_TO_LOCATION``
still does its own job (placing the marker).
"""

import uuid

from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.geodata.conversation_context import ConversationContext
from cwym_agent.map_commands import (
    ActivateLayerCommand,
    ActivateLayerPayload,
    ApplyFilterCommand,
    ApplyFilterPayload,
    Coordinates,
    DeactivateLayerCommand,
    DeactivateLayerPayload,
    DrawBoundaryCommand,
    DrawBoundaryPayload,
    FilterAreaPayload,
    FilterConditionPayload,
    FilterSpecPayload,
    NamedLocation,
    NavigateToLocationCommand,
    NavigateToLocationPayload,
    OpenToolCommand,
    OpenToolPayload,
    ZoomToExtentCommand,
    ZoomToExtentPayload,
)

# Action labels that map straight to ACTIVATE_LAYER/NAVIGATE_TO_LOCATION
# commands. "Öffne" and "Filter" get their own handling below (OPEN_TOOL,
# APPLY_FILTER respectively).
_ACTIONABLE_TYPES = {"Zeigen", "Navigieren"}

# Keyword -> OPEN_TOOL toolName, mirroring the keyword-matching style
# already used by action_detector (geodata/real_data_services.py). Not a
# new sub-agent - just enough to pick which Menu panel "öffne ..." means.
_TOOL_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("kontakt", "contact"), "CONTACT"),
    (("drucke", "drucken", "druck", "print"), "PRINT"),
    (("legende", "legend"), "LEGEND"),
    (("teile", "teilen", "share"), "SHARE"),
    (("über uns", "impressum", "about"), "ABOUT"),
]


def _detect_tool_name(query: str) -> str | None:
    lowered = query.lower()
    for keywords, tool_name in _TOOL_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return tool_name
    return None


@component
class CommandBuilderComponent:
    @component.output_types(commands=list)
    def run(
        self,
        aggregated: dict,
        context: ConversationContext | None = None,
        bridge: AGUIEventBridge | None = None,
    ):
        commands = self._build_commands(aggregated, context)

        if bridge:
            for cmd in commands:
                bridge.command(cmd)

        return {"commands": commands}

    def _build_commands(self, aggregated: dict, context: ConversationContext | None = None) -> list[dict]:
        # Checked before the action_detector-gated branches below,
        # unconditionally: Qwen3's plan can legitimately call filter_agent
        # without also calling action_detector (the intent is already
        # implied by calling filter_agent at all - confirmed live, a plan
        # for "Filtere Krankenhäuser in Wandsbek" called only
        # location_extractor/layer_resolver/filter_agent). A resolved
        # filter_result is on its own strong enough evidence of filter
        # intent; it shouldn't depend on action_detector also having run.
        if aggregated.get("filter_result") is not None:
            return self._build_filter_commands(aggregated, context)

        action = aggregated.get("action")
        if action is None:
            return []

        if action.action_type == "Öffne":
            tool_name = _detect_tool_name(aggregated.get("query", ""))
            if tool_name is None:
                return []
            return [
                OpenToolCommand(
                    command_id=str(uuid.uuid4()),
                    payload=OpenToolPayload(tool_name=tool_name),
                ).model_dump(by_alias=True)
            ]

        if action.action_type not in _ACTIONABLE_TYPES:
            return []

        commands: list[dict] = []

        layer = aggregated.get("layer")
        if layer:
            commands.append(
                ActivateLayerCommand(
                    command_id=str(uuid.uuid4()),
                    payload=ActivateLayerPayload(layer_ids=[layer.layer_id]),
                ).model_dump(by_alias=True)
            )

        locations = aggregated.get("locations") or []
        boundary_location = next((loc for loc in locations if loc.boundary), None)

        if boundary_location:
            commands.append(
                DrawBoundaryCommand(
                    command_id=str(uuid.uuid4()),
                    payload=DrawBoundaryPayload(
                        geometry=boundary_location.boundary,
                        label=boundary_location.name,
                        kind=boundary_location.kind,
                    ),
                ).model_dump(by_alias=True)
            )

        if locations:
            commands.append(
                NavigateToLocationCommand(
                    command_id=str(uuid.uuid4()),
                    payload=NavigateToLocationPayload(
                        locations=[
                            NamedLocation(
                                name=loc.name,
                                coordinates=Coordinates(lon=loc.longitude, lat=loc.latitude),
                                kind=loc.kind,
                            )
                            for loc in locations
                        ]
                    ),
                ).model_dump(by_alias=True)
            )

        extent_location = next((loc for loc in locations if loc.extent), None)
        if extent_location:
            commands.append(
                ZoomToExtentCommand(
                    command_id=str(uuid.uuid4()),
                    payload=ZoomToExtentPayload(bbox=extent_location.extent),
                ).model_dump(by_alias=True)
            )

        return commands

    def _build_filter_commands(
        self, aggregated: dict, context: ConversationContext | None = None
    ) -> list[dict]:
        """A "Filter" turn stands alone: shows only FilterAgent's matched
        features as an overlay, not the full unfiltered layer (unlike a
        "Zeigen"/"Navigieren" turn's ACTIVATE_LAYER) - activating the
        whole layer alongside a filtered subset would just show every
        feature twice, once highlighted and once not.

        On a follow-up, the target layer is usually already active from
        the previous turn (cmym-agent-architecture.md Section 10.7) - in
        that case DEACTIVATE_LAYER for it is emitted first, from this
        same call, so the frontend never briefly shows both the full
        layer and the filtered overlay together."""

        filter_result = aggregated.get("filter_result")
        if filter_result is None:
            return []

        total_matched = filter_result.number_matched
        shown = len(filter_result.features.get("features", []))
        truncated = total_matched is not None and total_matched > shown

        commands: list[dict] = []

        active_layer_ids = context.map_state.active_layer_ids if context else []
        if filter_result.layer_id in active_layer_ids:
            commands.append(
                DeactivateLayerCommand(
                    command_id=str(uuid.uuid4()),
                    payload=DeactivateLayerPayload(layer_ids=[filter_result.layer_id]),
                ).model_dump(by_alias=True)
            )

        filter_spec = None
        if filter_result.conditions or filter_result.area_kind:
            area = (
                FilterAreaPayload(name=filter_result.label, kind=filter_result.area_kind)
                if filter_result.label and filter_result.area_kind
                else None
            )
            filter_spec = FilterSpecPayload(
                conditions=[
                    FilterConditionPayload(field=c.field, operator=c.operator, value=c.value)
                    for c in filter_result.conditions
                ],
                area=area,
            )

        commands.append(
            ApplyFilterCommand(
                command_id=str(uuid.uuid4()),
                payload=ApplyFilterPayload(
                    layer_id=filter_result.layer_id,
                    features=filter_result.features,
                    label=filter_result.label,
                    total_matched=total_matched,
                    truncated=truncated,
                    filter_spec=filter_spec,
                ),
            ).model_dump(by_alias=True)
        )

        # Found live: a spatially-scoped filter (e.g. "Fahrradstationen
        # in Wandsbek") pushed the matched features as an overlay with
        # no ZOOM_TO_EXTENT at all - the results could land anywhere
        # relative to whatever the map already happened to be showing,
        # with nothing moving the view to them. Reuses the same
        # location + extent that (when present) drove FilterAgent's own
        # S_INTERSECTS predicate.
        locations = aggregated.get("locations") or []
        extent_location = next((loc for loc in locations if loc.extent), None)
        if extent_location:
            commands.append(
                ZoomToExtentCommand(
                    command_id=str(uuid.uuid4()),
                    payload=ZoomToExtentPayload(bbox=extent_location.extent),
                ).model_dump(by_alias=True)
            )

        return commands
