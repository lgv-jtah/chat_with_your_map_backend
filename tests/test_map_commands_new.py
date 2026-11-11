"""The MapCommands added for charts, suggestions, tours and highlighting
(CHAT-14, architecture 14.5): validation and wire format."""

import pytest
from pydantic import TypeAdapter, ValidationError

from cwym_agent.map_commands import COMMAND_TYPES, LEGACY_COMMAND_TYPES, MapCommand

_ADAPTER = TypeAdapter(MapCommand)
_POINT = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [10.0, 53.6]}, "properties": {}}


def _parse(command: dict):
    return _ADAPTER.validate_python({"commandId": "c1", **command})


def test_command_types():
    assert {"SHOW_CHART", "SHOW_SUGGESTIONS", "START_TOUR", "HIGHLIGHT_FEATURES"} <= set(COMMAND_TYPES)
    assert "HIGHLIGHT_FEATURE" not in COMMAND_TYPES  # the unused stub is replaced
    assert LEGACY_COMMAND_TYPES <= set(COMMAND_TYPES)
    assert not {"SHOW_CHART", "SHOW_SUGGESTIONS", "START_TOUR", "HIGHLIGHT_FEATURES"} & LEGACY_COMMAND_TYPES


class TestShowChart:
    def test_wire_format(self):
        command = _parse({
            "type": "SHOW_CHART",
            "payload": {
                "title": "Energieeffizienzklasse: Verteilung",
                "data": [{"label": "H", "value": 105, "color": "#991b1b"}, {"label": "keine Angabe", "value": 36, "kind": "missing"}],
                "total": 235, "datasetKey": "buildings", "attribute": "energieeffizienzklasse",
            },
        })

        assert command.model_dump(by_alias=True) == {
            "commandId": "c1",
            "type": "SHOW_CHART",
            "payload": {
                "title": "Energieeffizienzklasse: Verteilung",
                "chartType": "bar",
                "data": [
                    {"label": "H", "value": 105.0, "color": "#991b1b", "kind": "value"},
                    {"label": "keine Angabe", "value": 36.0, "color": None, "kind": "missing"},
                ],
                "total": 235,
                "datasetKey": "buildings",
                "attribute": "energieeffizienzklasse",
            },
        }

    @pytest.mark.parametrize("payload", [
        {"title": "t", "data": []},
        {"title": "t", "data": [{"label": "a", "value": 1, "color": "red"}]},
        {"title": "t", "data": [{"label": "a", "value": 1}], "chartType": "pie"},
        {"title": "", "data": [{"label": "a", "value": 1}]},
    ])
    def test_invalid(self, payload):
        with pytest.raises(ValidationError):
            _parse({"type": "SHOW_CHART", "payload": payload})


class TestShowSuggestions:
    def test_at_most_three_short_items(self):
        assert _parse({"type": "SHOW_SUGGESTIONS", "payload": {"items": ["a", "b", "c"]}}).payload.items == ["a", "b", "c"]
        for items in ([], ["a", "b", "c", "d"], ["x" * 121], [""]):
            with pytest.raises(ValidationError):
                _parse({"type": "SHOW_SUGGESTIONS", "payload": {"items": items}})


class TestHighlightFeatures:
    def test_defaults_to_query_style(self):
        command = _parse({"type": "HIGHLIGHT_FEATURES", "payload": {"features": {"type": "FeatureCollection", "features": [_POINT]}}})

        assert command.payload.style == "query"
        with pytest.raises(ValidationError):
            _parse({"type": "HIGHLIGHT_FEATURES", "payload": {"features": {}, "style": "bold"}})


class TestStartTour:
    def _tour(self, commands):
        return {
            "type": "START_TOUR",
            "payload": {
                "tourId": "harthof",
                "steps": [{"delayMs": 0, "narration": "Willkommen im Harthof.", "commands": commands}],
                "followUp": "Möchtest du etwas genauer erkunden?",
                "suggestions": ["Zeig die Energieklassen"],
            },
        }

    def test_steps_carry_validated_commands(self):
        tour = _parse(self._tour([{"commandId": "s1", "type": "ACTIVATE_LAYER", "payload": {"layerIds": ["x"]}}]))

        assert tour.payload.steps[0].commands[0].type == "ACTIVATE_LAYER"
        assert tour.model_dump(by_alias=True)["payload"]["steps"][0]["delayMs"] == 0

    def test_invalid_step_commands(self):
        with pytest.raises(ValidationError):
            _parse(self._tour([{"commandId": "s1", "type": "ACTIVATE_LAYER", "payload": {}}]))
        with pytest.raises(ValidationError, match="can't start another tour"):
            _parse(self._tour([self._tour([]) | {"commandId": "s2"}]))
