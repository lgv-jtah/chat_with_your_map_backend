"""Unit tests for ``CommandBuilderComponent._build_commands``."""

from cwym_agent.components.command_builder import CommandBuilderComponent
from cwym_agent.geodata.types import ActionMatch, LayerMatch, LocationMatch

_WANDSBEK_BOUNDARY = {
    "type": "Polygon",
    "coordinates": [[[10.0, 53.5], [10.1, 53.5], [10.1, 53.6], [10.0, 53.6], [10.0, 53.5]]],
}

_DISTRICT_LOCATION = LocationMatch(
    name="Wandsbek", latitude=53.5760029, longitude=10.0755348, confidence=0.95, boundary=_WANDSBEK_BOUNDARY
)
_STREET_LOCATION = LocationMatch(
    name="Mönckebergstraße", latitude=53.55, longitude=10.0, confidence=0.9, boundary=None
)
_LAYER = LayerMatch(name="StadtRAD-Stationen Hamburg", layer_id="27764", confidence=0.92)


def _aggregated(action_type: str | None, layer=None, locations=None, query="") -> dict:
    return {
        "query": query,
        "locations": locations or [],
        "layer": layer,
        "action": ActionMatch(action_type, 0.9) if action_type else None,
    }


builder = CommandBuilderComponent()


class TestBuildCommands:
    def test_layer_and_district_location_builds_all_four_commands_in_order(self):
        aggregated = _aggregated("Zeigen", layer=_LAYER, locations=[_DISTRICT_LOCATION])

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == [
            "ACTIVATE_LAYER",
            "DRAW_BOUNDARY",
            "NAVIGATE_TO_LOCATION",
            "APPLY_SPATIAL_FILTER",
        ]
        assert commands[0]["payload"]["layerIds"] == ["27764"]
        assert commands[1]["payload"]["geometry"] == _WANDSBEK_BOUNDARY
        assert commands[1]["payload"]["label"] == "Wandsbek"
        assert commands[2]["payload"]["locations"][0]["name"] == "Wandsbek"
        assert commands[3]["payload"]["layerIds"] == ["27764"]
        assert commands[3]["payload"]["geometry"] == _WANDSBEK_BOUNDARY
        assert commands[3]["payload"]["mode"] == "intersects"

    def test_layer_and_street_location_builds_only_activate_and_navigate(self):
        aggregated = _aggregated("Zeigen", layer=_LAYER, locations=[_STREET_LOCATION])

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["ACTIVATE_LAYER", "NAVIGATE_TO_LOCATION"]

    def test_district_location_without_layer_builds_boundary_and_navigate_no_spatial_filter(self):
        aggregated = _aggregated("Zeigen", layer=None, locations=[_DISTRICT_LOCATION])

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["DRAW_BOUNDARY", "NAVIGATE_TO_LOCATION"]

    def test_filter_action_produces_zero_commands_even_with_district_location(self):
        aggregated = _aggregated("Filter", layer=_LAYER, locations=[_DISTRICT_LOCATION])

        assert builder._build_commands(aggregated) == []

    def test_oeffne_action_path_unaffected_by_boundary_logic(self):
        aggregated = _aggregated("Öffne", layer=_LAYER, locations=[_DISTRICT_LOCATION], query="Kontaktformular öffnen")

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["OPEN_TOOL"]
        assert commands[0]["payload"]["toolName"] == "CONTACT"
