"""Unit tests for ``CommandBuilderComponent._build_commands``."""

from cwym_agent.components.command_builder import CommandBuilderComponent
from cwym_agent.geodata.conversation_context import ConversationContext, FilterStateIn, MapStateIn
from cwym_agent.geodata.types import (
    ActionMatch,
    AnalysisResult,
    FilterCondition,
    FilterResult,
    LayerMatch,
    LocationMatch,
    ResultLayer,
)

_WANDSBEK_BOUNDARY = {
    "type": "Polygon",
    "coordinates": [[[10.0, 53.5], [10.1, 53.5], [10.1, 53.6], [10.0, 53.6], [10.0, 53.5]]],
}

_WANDSBEK_EXTENT = (10.0, 53.5, 10.1, 53.6)

_DISTRICT_LOCATION = LocationMatch(
    name="Wandsbek",
    latitude=53.5760029,
    longitude=10.0755348,
    confidence=0.95,
    boundary=_WANDSBEK_BOUNDARY,
    extent=_WANDSBEK_EXTENT,
    kind="bezirk",
)
_STREET_LOCATION = LocationMatch(
    name="Mönckebergstraße", latitude=53.55, longitude=10.0, confidence=0.9, boundary=None
)
_HAMBURG_WIDE_LOCATION = LocationMatch(
    name="Hamburg", latitude=53.5511, longitude=9.9937, confidence=0.9, extent=(9.1, 52.8, 10.9, 54.1)
)
_LAYER = LayerMatch(name="StadtRAD-Stationen Hamburg", layer_id="27764", confidence=0.92)
_FEATURES = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}}]}


def _aggregated(
    action_type: str | None,
    layer=None,
    locations=None,
    query="",
    filter_result=None,
    deactivate_layer_ids=None,
) -> dict:
    return {
        "query": query,
        "locations": locations or [],
        "layer": layer,
        "action": ActionMatch(action_type, 0.9, deactivate_layer_ids or []) if action_type else None,
        "filter_result": filter_result,
    }


builder = CommandBuilderComponent()


class TestBuildCommands:
    def test_layer_and_district_location_builds_four_commands_in_order(self):
        aggregated = _aggregated("Zeigen", layer=_LAYER, locations=[_DISTRICT_LOCATION])

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == [
            "ACTIVATE_LAYER",
            "DRAW_BOUNDARY",
            "NAVIGATE_TO_LOCATION",
            "ZOOM_TO_EXTENT",
        ]
        assert commands[0]["payload"]["layerIds"] == ["27764"]
        assert commands[1]["payload"]["geometry"] == _WANDSBEK_BOUNDARY
        assert commands[1]["payload"]["label"] == "Wandsbek"
        assert commands[1]["payload"]["kind"] == "bezirk"
        assert commands[2]["payload"]["locations"][0]["name"] == "Wandsbek"
        assert commands[2]["payload"]["locations"][0]["kind"] == "bezirk"
        assert tuple(commands[3]["payload"]["bbox"]) == _WANDSBEK_EXTENT

    def test_layer_and_street_location_builds_only_activate_and_navigate(self):
        aggregated = _aggregated("Zeigen", layer=_LAYER, locations=[_STREET_LOCATION])

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["ACTIVATE_LAYER", "NAVIGATE_TO_LOCATION"]

    def test_district_location_without_layer_builds_boundary_navigate_and_extent(self):
        aggregated = _aggregated("Zeigen", layer=None, locations=[_DISTRICT_LOCATION])

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["DRAW_BOUNDARY", "NAVIGATE_TO_LOCATION", "ZOOM_TO_EXTENT"]

    def test_location_with_extent_but_no_boundary_still_zooms_to_extent(self):
        """The Hamburg-wide fallback (RealDataServices' _DEFAULT_LOCATION)
        has no real boundary geometry to draw, but does have a hardcoded
        extent - DRAW_BOUNDARY must not fire, ZOOM_TO_EXTENT still
        should."""

        aggregated = _aggregated("Zeigen", layer=None, locations=[_HAMBURG_WIDE_LOCATION])

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["NAVIGATE_TO_LOCATION", "ZOOM_TO_EXTENT"]
        assert tuple(commands[1]["payload"]["bbox"]) == (9.1, 52.8, 10.9, 54.1)

    def test_location_without_extent_or_boundary_builds_no_extras(self):
        aggregated = _aggregated("Zeigen", layer=None, locations=[_STREET_LOCATION])

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["NAVIGATE_TO_LOCATION"]

    def test_oeffne_action_path_unaffected_by_boundary_logic(self):
        aggregated = _aggregated("Öffne", layer=_LAYER, locations=[_DISTRICT_LOCATION], query="Kontaktformular öffnen")

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["OPEN_TOOL"]
        assert commands[0]["payload"]["toolName"] == "CONTACT"


class TestDeactivateLayer:
    """"zeige U-Bahn, aber nicht mehr Fahrradstationen" - Section 10.5
    rule 5."""

    def test_named_active_layer_deactivated_before_activating_new_one(self):
        aggregated = _aggregated("Zeigen", layer=_LAYER, deactivate_layer_ids=["99999"])
        context = ConversationContext(map_state=MapStateIn(active_layer_ids=["99999"]), history=[])

        commands = builder._build_commands(aggregated, context)

        assert [cmd["type"] for cmd in commands] == ["DEACTIVATE_LAYER", "ACTIVATE_LAYER"]
        assert commands[0]["payload"]["layerIds"] == ["99999"]
        assert commands[1]["payload"]["layerIds"] == ["27764"]

    def test_ordinary_zeigen_turn_deactivates_nothing(self):
        """No deactivate_layer_ids at all - a new layer only ever adds
        to what's active, it doesn't silently drop others."""

        aggregated = _aggregated("Zeigen", layer=_LAYER)
        context = ConversationContext(map_state=MapStateIn(active_layer_ids=["99999"]), history=[])

        commands = builder._build_commands(aggregated, context)

        assert [cmd["type"] for cmd in commands] == ["ACTIVATE_LAYER"]

    def test_deactivate_id_not_actually_active_is_ignored(self):
        """Defensive: don't trust the planner's output blindly - only
        deactivate ids actually in the live map state, same posture as
        build_context's own id validation."""

        aggregated = _aggregated("Zeigen", layer=_LAYER, deactivate_layer_ids=["12345"])
        context = ConversationContext(map_state=MapStateIn(active_layer_ids=["99999"]), history=[])

        commands = builder._build_commands(aggregated, context)

        assert [cmd["type"] for cmd in commands] == ["ACTIVATE_LAYER"]

    def test_no_context_deactivates_nothing(self):
        aggregated = _aggregated("Zeigen", layer=_LAYER, deactivate_layer_ids=["99999"])

        commands = builder._build_commands(aggregated, context=None)

        assert [cmd["type"] for cmd in commands] == ["ACTIVATE_LAYER"]

    def test_multiple_deactivate_ids_all_emitted(self):
        aggregated = _aggregated("Zeigen", layer=_LAYER, deactivate_layer_ids=["11111", "22222"])
        context = ConversationContext(
            map_state=MapStateIn(active_layer_ids=["11111", "22222", "33333"]), history=[]
        )

        commands = builder._build_commands(aggregated, context)

        assert [cmd["type"] for cmd in commands] == ["DEACTIVATE_LAYER", "DEACTIVATE_LAYER", "ACTIVATE_LAYER"]


class TestBuildFilterCommands:
    def test_filter_result_produces_apply_filter_command(self):
        filter_result = FilterResult(
            layer_id="27714", features=_FEATURES, number_matched=1, label="Wandsbek"
        )
        aggregated = _aggregated("Filter", layer=_LAYER, filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["APPLY_FILTER"]
        assert commands[0]["payload"]["layerId"] == "27714"
        assert commands[0]["payload"]["features"] == _FEATURES
        assert commands[0]["payload"]["label"] == "Wandsbek"
        assert commands[0]["payload"]["totalMatched"] == 1
        assert commands[0]["payload"]["truncated"] is False

    def test_spatially_scoped_filter_also_zooms_to_the_location_extent(self):
        """Found live: "Fahrradstationen in Wandsbek" pushed matched
        features onto the map with no view change at all - the results
        could land anywhere relative to whatever was already on screen."""

        filter_result = FilterResult(
            layer_id="27714", features=_FEATURES, number_matched=1, label="Wandsbek", area_kind="bezirk"
        )
        aggregated = _aggregated("Filter", layer=_LAYER, locations=[_DISTRICT_LOCATION], filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["APPLY_FILTER", "ZOOM_TO_EXTENT"]
        assert tuple(commands[1]["payload"]["bbox"]) == _WANDSBEK_EXTENT

    def test_filter_without_a_location_does_not_zoom(self):
        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        aggregated = _aggregated("Filter", layer=_LAYER, filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["APPLY_FILTER"]

    def test_filter_with_location_but_no_extent_does_not_zoom(self):
        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        aggregated = _aggregated("Filter", layer=_LAYER, locations=[_STREET_LOCATION], filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["APPLY_FILTER"]

    def test_more_matched_than_shown_marks_truncated(self):
        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=500)
        aggregated = _aggregated("Filter", layer=_LAYER, filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert commands[0]["payload"]["totalMatched"] == 500
        assert commands[0]["payload"]["truncated"] is True

    def test_unknown_number_matched_is_not_truncated(self):
        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=None)
        aggregated = _aggregated("Filter", layer=_LAYER, filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert commands[0]["payload"]["totalMatched"] is None
        assert commands[0]["payload"]["truncated"] is False

    def test_filter_result_produces_apply_filter_even_without_action_detector(self):
        """Qwen3's plan can call filter_agent without also calling
        action_detector (confirmed live: a plan for "Filtere
        Krankenhäuser in Wandsbek" called only location_extractor/
        layer_resolver/filter_agent) - a resolved filter_result must not
        depend on action_detector having run too."""

        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        aggregated = _aggregated(None, layer=_LAYER, filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == ["APPLY_FILTER"]

    def test_filter_action_without_filter_result_produces_zero_commands(self):
        """FilterAgent found nothing to filter by, or the layer wasn't
        OGC API Features-filterable - a legitimate zero-MapCommand turn,
        same principle as the "Öffne"/unresolved-action cases."""

        aggregated = _aggregated("Filter", layer=_LAYER, locations=[_DISTRICT_LOCATION], filter_result=None)

        assert builder._build_commands(aggregated) == []

    def test_filter_result_does_not_also_activate_the_layer(self):
        """A Filter turn shows only the matched-features overlay, not
        the full unfiltered layer - see _build_filter_commands."""

        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        aggregated = _aggregated("Filter", layer=_LAYER, filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert "ACTIVATE_LAYER" not in [cmd["type"] for cmd in commands]


class TestFilterCommandsWithContext:
    def test_target_layer_already_active_deactivates_it_before_filtering(self):
        """Section 10.7: a follow-up filtering an already-shown layer
        must not display the full layer and the filtered overlay at
        once."""

        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        aggregated = _aggregated("Filter", filter_result=filter_result)
        context = ConversationContext(
            map_state=MapStateIn(active_layer_ids=["27714"]), history=[]
        )

        commands = builder._build_commands(aggregated, context)

        assert [cmd["type"] for cmd in commands] == ["DEACTIVATE_LAYER", "APPLY_FILTER"]
        assert commands[0]["payload"]["layerIds"] == ["27714"]

    def test_target_layer_not_active_skips_deactivate(self):
        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        aggregated = _aggregated("Filter", filter_result=filter_result)
        context = ConversationContext(map_state=MapStateIn(active_layer_ids=["99999"]), history=[])

        commands = builder._build_commands(aggregated, context)

        assert [cmd["type"] for cmd in commands] == ["APPLY_FILTER"]

    def test_no_context_behaves_like_before(self):
        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        aggregated = _aggregated("Filter", filter_result=filter_result)

        commands = builder._build_commands(aggregated, context=None)

        assert [cmd["type"] for cmd in commands] == ["APPLY_FILTER"]

    def test_filter_spec_carries_conditions_and_area(self):
        filter_result = FilterResult(
            layer_id="27714",
            features=_FEATURES,
            number_matched=1,
            label="Wandsbek",
            area_kind="bezirk",
            conditions=[FilterCondition(field="schulform", operator="eq", value="Gymnasium")],
        )
        aggregated = _aggregated("Filter", filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        filter_spec = commands[0]["payload"]["filterSpec"]
        assert filter_spec["conditions"] == [{"field": "schulform", "operator": "eq", "value": "Gymnasium"}]
        assert filter_spec["area"] == {"name": "Wandsbek", "kind": "bezirk"}

    def test_no_conditions_or_area_yields_no_filter_spec(self):
        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        aggregated = _aggregated("Filter", filter_result=filter_result)

        commands = builder._build_commands(aggregated)

        assert commands[0]["payload"]["filterSpec"] is None


def _filtered_context(*layer_ids: str) -> ConversationContext:
    return ConversationContext(
        map_state=MapStateIn(
            filters=[FilterStateIn.model_validate({"layerId": i, "filterSpec": {}}) for i in layer_ids]
        ),
        history=[],
    )


def _clear_action(action_type: str, *layer_ids: str) -> ActionMatch:
    return ActionMatch(action_type, 0.9, clear_filter_layer_ids=list(layer_ids))


class TestClearFilter:
    """"zeig wieder alle" - Section 10.5 rule 6."""

    def test_clears_filter_and_restores_full_layer(self):
        aggregated = {**_aggregated(None), "action": _clear_action("Zeigen", "1732")}

        commands = builder._build_commands(aggregated, _filtered_context("1732"))

        assert [cmd["type"] for cmd in commands] == ["CLEAR_FILTER", "ACTIVATE_LAYER"]
        assert commands[0]["payload"] == {"layerId": "1732"}
        assert commands[1]["payload"]["layerIds"] == ["1732"]

    def test_filter_keyword_action_still_clears(self):
        """"Filter entfernen" keyword-detects as "Filter", which isn't in
        _ACTIONABLE_TYPES - the clear must not depend on that."""

        aggregated = {**_aggregated(None), "action": _clear_action("Filter", "1732")}

        commands = builder._build_commands(aggregated, _filtered_context("1732"))

        assert [cmd["type"] for cmd in commands] == ["CLEAR_FILTER", "ACTIVATE_LAYER"]

    def test_id_without_active_filter_is_ignored(self):
        aggregated = {**_aggregated(None), "action": _clear_action("Zeigen", "99999")}

        assert builder._build_commands(aggregated, _filtered_context("1732")) == []

    def test_no_context_clears_nothing(self):
        aggregated = {**_aggregated(None), "action": _clear_action("Zeigen", "1732")}

        assert builder._build_commands(aggregated) == []

    def test_resolved_layer_same_as_cleared_is_not_activated_twice(self):
        layer = LayerMatch(name="Schulen Hamburg", layer_id="1732", confidence=0.9)
        aggregated = {**_aggregated(None, layer=layer), "action": _clear_action("Zeigen", "1732")}

        commands = builder._build_commands(aggregated, _filtered_context("1732"))

        assert [cmd["type"] for cmd in commands] == ["CLEAR_FILTER", "ACTIVATE_LAYER"]

    def test_duplicate_ids_cleared_once(self):
        aggregated = {**_aggregated(None), "action": _clear_action("Zeigen", "1732", "1732")}

        commands = builder._build_commands(aggregated, _filtered_context("1732"))

        assert [cmd["type"] for cmd in commands] == ["CLEAR_FILTER", "ACTIVATE_LAYER"]

    def test_ordinary_zeigen_turn_clears_nothing(self):
        aggregated = _aggregated("Zeigen", layer=_LAYER)

        commands = builder._build_commands(aggregated, _filtered_context("1732"))

        assert [cmd["type"] for cmd in commands] == ["ACTIVATE_LAYER"]


def _result_layer(key, role, bbox=None):
    return ResultLayer(
        key=key,
        label=key,
        role=role,
        features={"type": "FeatureCollection", "features": [{"type": "Feature"}]},
        feature_count=1,
        total_features=1,
        bbox=bbox,
    )


class TestAnalysisCommands:
    """UDP agent analysis turns - cmym-agent-architecture.md Section 13."""

    def test_clears_then_adds_context_before_result_then_zooms(self):
        analysis = AnalysisResult(
            answer="a",
            layers=[
                _result_layer("stations", "result", (9.93, 53.55, 9.95, 53.56)),
                _result_layer("altona", "context", (9.8, 53.5, 10.0, 53.6)),
            ],
        )
        aggregated = {**_aggregated(None), "analysis_result": analysis}

        commands = builder._build_commands(aggregated)

        assert [cmd["type"] for cmd in commands] == [
            "CLEAR_RESULT_LAYERS",
            "ADD_RESULT_LAYER",
            "ADD_RESULT_LAYER",
            "ZOOM_TO_EXTENT",
        ]
        assert [cmd["payload"]["layerKey"] for cmd in commands[1:3]] == ["altona", "stations"]
        assert commands[2]["payload"]["role"] == "result"
        assert commands[2]["payload"]["features"]["type"] == "FeatureCollection"
        assert tuple(commands[3]["payload"]["bbox"]) == (9.8, 53.5, 10.0, 53.6)

    def test_single_point_extent_padded(self):
        analysis = AnalysisResult(answer="a", layers=[_result_layer("s", "result", (9.93, 53.55, 9.93, 53.55))])

        commands = builder._build_commands({**_aggregated(None), "analysis_result": analysis})

        min_x, min_y, max_x, max_y = commands[-1]["payload"]["bbox"]
        assert max_x - min_x >= 0.0099 and max_y - min_y >= 0.0099
        assert (min_x + max_x) / 2 == 9.93

    def test_no_bbox_means_no_zoom(self):
        analysis = AnalysisResult(answer="a", layers=[_result_layer("s", "result")])

        commands = builder._build_commands({**_aggregated(None), "analysis_result": analysis})

        assert [cmd["type"] for cmd in commands] == ["CLEAR_RESULT_LAYERS", "ADD_RESULT_LAYER"]

    def test_no_layers_builds_nothing_and_keeps_previous_result(self):
        """A clarifying question or a failed analysis must not wipe the map."""

        for analysis in (AnalysisResult(answer="Welche meinen Sie?"), AnalysisResult(answer=None, error="x")):
            assert builder._build_commands({**_aggregated("Zeigen", layer=_LAYER), "analysis_result": analysis}) == []

    def test_analysis_takes_precedence_over_filter_and_show(self):
        filter_result = FilterResult(layer_id="27714", features=_FEATURES, number_matched=1)
        analysis = AnalysisResult(answer="a", layers=[_result_layer("s", "result")])
        aggregated = {
            **_aggregated("Zeigen", layer=_LAYER, filter_result=filter_result),
            "analysis_result": analysis,
        }

        types = [cmd["type"] for cmd in builder._build_commands(aggregated)]

        assert types == ["CLEAR_RESULT_LAYERS", "ADD_RESULT_LAYER"]
