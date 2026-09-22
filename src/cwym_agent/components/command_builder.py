"""``CommandBuilderComponent``: turns aggregated sub-agent results into an
ordered list of typed ``MapCommand``s (see ``cwym_agent.map_commands``),
and streams each one individually as its own AG-UI ``TOOL_CALL_*`` cycle
via the bridge, so the frontend can execute ``ACTIVATE_LAYER`` while
``NAVIGATE_TO_LOCATION`` is still being built, instead of waiting for a
single combined payload.

When a resolved location carries a district-level area boundary (see
``cwym-agent-architecture.md`` Section 9), the fixed build order is
``ACTIVATE_LAYER -> DRAW_BOUNDARY -> NAVIGATE_TO_LOCATION ->
APPLY_SPATIAL_FILTER`` - the boundary/spatial-filter emission is a
structural signal (layer + area-geometry location resolved together),
not tied to a specific ``action_type``; see Section 9.3 for why.
"""

import uuid

from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.map_commands import (
    ActivateLayerCommand,
    ActivateLayerPayload,
    ApplySpatialFilterCommand,
    ApplySpatialFilterPayload,
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
# commands. "Öffne" gets its own handling below (OPEN_TOOL). "Filter" is
# a valid action_detector output with no consuming logic yet (FilterAgent
# is explicitly out of scope here), so a turn resolving to it legitimately
# produces zero commands, per the architecture doc's Section 5 design
# principle.
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

        if layer and boundary_location:
            commands.append(
                ApplySpatialFilterCommand(
                    command_id=str(uuid.uuid4()),
                    payload=ApplySpatialFilterPayload(
                        layer_ids=[layer.layer_id],
                        geometry=boundary_location.boundary,
                        label=boundary_location.name,
                    ),
                ).model_dump(by_alias=True)
            )

        return commands
