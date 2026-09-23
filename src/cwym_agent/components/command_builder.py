"""``CommandBuilderComponent``: turns aggregated sub-agent results into an
ordered list of typed ``MapCommand``s (see ``cwym_agent.map_commands``),
and streams each one individually as its own AG-UI ``TOOL_CALL_*`` cycle
via the bridge, so the frontend can execute ``ACTIVATE_LAYER`` while
``NAVIGATE_TO_LOCATION`` is still being built, instead of waiting for a
single combined payload.

When a resolved location carries a district-level area boundary, the
build order for a "Zeigen"/"Navigieren" turn is ``ACTIVATE_LAYER ->
DRAW_BOUNDARY -> NAVIGATE_TO_LOCATION`` - draws the district for visual
context, but (unlike an earlier version of this component) does not
spatially restrict the activated layer's own features; that's now
``FilterAgent``'s job for a "Filter" turn, via real server-side OGC API
Features CQL2 queries rather than client-side masking (see
``cwym-agent-architecture.md`` Section 9).
"""

import uuid

from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.map_commands import (
    ActivateLayerCommand,
    ActivateLayerPayload,
    ApplyFilterCommand,
    ApplyFilterPayload,
    Coordinates,
    DrawBoundaryCommand,
    DrawBoundaryPayload,
    NamedLocation,
    NavigateToLocationCommand,
    NavigateToLocationPayload,
    OpenToolCommand,
    OpenToolPayload,
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
    def run(self, aggregated: dict, bridge: AGUIEventBridge | None = None):
        commands = self._build_commands(aggregated)

        if bridge:
            for cmd in commands:
                bridge.command(cmd)

        return {"commands": commands}

    def _build_commands(self, aggregated: dict) -> list[dict]:
        # Checked before the action_detector-gated branches below,
        # unconditionally: Qwen3's plan can legitimately call filter_agent
        # without also calling action_detector (the intent is already
        # implied by calling filter_agent at all - confirmed live, a plan
        # for "Filtere Krankenhäuser in Wandsbek" called only
        # location_extractor/layer_resolver/filter_agent). A resolved
        # filter_result is on its own strong enough evidence of filter
        # intent; it shouldn't depend on action_detector also having run.
        if aggregated.get("filter_result") is not None:
            return self._build_filter_commands(aggregated)

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
                            )
                            for loc in locations
                        ]
                    ),
                ).model_dump(by_alias=True)
            )

        return commands

    def _build_filter_commands(self, aggregated: dict) -> list[dict]:
        """A "Filter" turn stands alone: shows only FilterAgent's matched
        features as an overlay, not the full unfiltered layer (unlike a
        "Zeigen"/"Navigieren" turn's ACTIVATE_LAYER) - activating the
        whole layer alongside a filtered subset would just show every
        feature twice, once highlighted and once not."""

        filter_result = aggregated.get("filter_result")
        if filter_result is None:
            return []

        return [
            ApplyFilterCommand(
                command_id=str(uuid.uuid4()),
                payload=ApplyFilterPayload(
                    layer_id=filter_result.layer_id,
                    features=filter_result.features,
                    label=filter_result.label,
                ),
            ).model_dump(by_alias=True)
        ]
