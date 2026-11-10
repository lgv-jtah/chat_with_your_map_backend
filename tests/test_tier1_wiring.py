"""Tier 1 through the pipeline components (CHAT-11): planner arg ->
executor -> aggregator -> map commands -> fixed reply."""

import pytest
from fixtures.datasets import buildings_profile

from cwym_agent.components.agent_executor import AgentExecutorComponent, _tool_call_result_view
from cwym_agent.components.command_builder import CommandBuilderComponent
from cwym_agent.components.result_aggregator import ResultAggregatorComponent
from cwym_agent.components.text_response import TextResponseComponent
from cwym_agent.geodata.analysis import Computed, compute
from cwym_agent.geodata.conversation_context import ConversationContext, MapStateIn
from cwym_agent.geodata.types import DatasetAnswer, FilterCondition, LayerMatch, LocationMatch

_LAYER = LayerMatch("Gebäude", "test_gebaeude", 0.9)
_BOUNDARY = {"type": "Polygon", "coordinates": [[[10.0, 53.6], [10.01, 53.6], [10.01, 53.61], [10.0, 53.6]]]}
_AREA = LocationMatch("Nordviertel", 53.6, 10.0, 0.9, boundary=_BOUNDARY, extent=(10.0, 53.6, 10.01, 53.61), kind="stadtteil")


def _feature(gebid, x=10.001, y=53.601):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]}, "properties": {"gebid": gebid}}


class _Services:
    def __init__(self, answer: DatasetAnswer):
        self.answer = answer
        self.calls = []

    def resolve_locations(self, query):
        return [_AREA]

    def resolve_layer(self, query, allowed_layer_ids=None):
        return _LAYER

    def analyze_dataset(self, query, operation, layer, location=None):
        self.calls.append((query, operation, layer, location))
        return self.answer

    def apply_filter(self, *args, **kwargs):
        raise AssertionError("an analysis must not filter")


def _steps(operation):
    return {"steps": [
        {"agent_name": "layer_resolver", "args": {"query": "Gebäude"}},
        {"agent_name": "location_extractor", "args": {"query": "Nordviertel"}},
        {"agent_name": "filter_agent", "args": {"query": "Wie viele Gebäude im Nordviertel?", "operation": operation}},
    ]}


def _count_answer(total=3, features=None):
    features = features if features is not None else [_feature("B01"), _feature("B02"), _feature("B03")]
    return DatasetAnswer(
        operation="count", status="ok", computed=Computed("count", total=total), dataset_label={"de": "Gebäude", "en": "buildings"},
        layer_id="test_gebaeude", layer_name="Gebäude", features=features, area_name="Nordviertel", area_kind="stadtteil",
        conditions=[FilterCondition("eigentuemer", "eq", "Wohnbau")], labels={"eigentuemer": {"de": "Eigentümer", "en": "owner"}},
    )


def _run(answer, operation="count", context=None):
    services = _Services(answer)
    log = AgentExecutorComponent(services).run(_steps(operation), context=context)["execution_log"]
    aggregated = ResultAggregatorComponent().run(log, "q")["aggregated"]
    commands = CommandBuilderComponent()._build_commands(aggregated, context)
    return services, aggregated, commands


class TestExecutorAndAggregator:
    def test_operation_goes_to_the_analysis(self):
        services, aggregated, _ = _run(_count_answer())

        [(query, operation, layer, location)] = services.calls
        assert (operation, layer, location) == ("count", _LAYER, _AREA)
        assert aggregated["dataset_answer"].computed.total == 3
        assert aggregated["filter_result"] is None and not aggregated["filter_failed"]

    def test_unknown_operation_is_a_normal_filter(self):
        class Filtering(_Services):
            def apply_filter(self, *args, **kwargs):
                return None

        services = Filtering(_count_answer())
        AgentExecutorComponent(services).run(_steps("median"))
        assert services.calls == []

    def test_trace_view_carries_numbers_not_features(self):
        from dataclasses import asdict

        answer = _count_answer()
        answer.computed = compute("top", [{"properties": {"b": "1"}}] * 3, "b", _number_attribute(), n=2)
        view = _tool_call_result_view("filter_agent", asdict(answer))

        assert "features" not in view and view["feature_count"] == 3
        assert "rows" not in view["computed"] and view["computed"]["row_count"] == 2


def _number_attribute():
    from cwym_agent.city_profile import AttributeConfig

    return AttributeConfig.model_validate({"label": {"de": "B"}, "type": "number"})


class TestCommands:
    def test_count_shows_the_features_with_the_total(self):
        _, _, commands = _run(_count_answer())

        assert [c["type"] for c in commands] == ["APPLY_FILTER", "ZOOM_TO_EXTENT"]
        payload = commands[0]["payload"]
        assert payload["layerId"] == "test_gebaeude" and payload["totalMatched"] == 3
        assert payload["filterSpec"]["area"] == {"name": "Nordviertel", "kind": "stadtteil"}
        assert tuple(commands[1]["payload"]["bbox"]) == (10.0, 53.6, 10.01, 53.61)

    def test_count_above_the_display_limit_is_marked_truncated(self):
        answer = _count_answer(total=900)
        answer.features_truncated = True

        _, _, commands = _run(answer)

        assert commands[0]["payload"]["totalMatched"] == 900 and commands[0]["payload"]["truncated"] is True

    def test_distribution_switches_on_the_display_layer(self):
        answer = DatasetAnswer(operation="distribution", status="ok", computed=Computed("distribution", total=12),
                               display_layer="test_energieklasse")
        context = ConversationContext(map_state=MapStateIn(), history=[])

        _, _, commands = _run(answer, "distribution", context)

        assert [c["type"] for c in commands] == ["ACTIVATE_LAYER", "ZOOM_TO_EXTENT"]
        assert commands[0]["payload"]["layerIds"] == ["test_energieklasse"]

    def test_lookup_without_area_zooms_to_the_feature(self):
        answer = DatasetAnswer(operation="lookup", status="ok", computed=Computed("lookup", total=12, lookup_key="Lindenweg 1"),
                               layer_id="test_gebaeude", features=[_feature("B01", 10.0, 53.6)])

        class NoArea(_Services):
            def resolve_locations(self, query):
                return []

        services = NoArea(answer)
        log = AgentExecutorComponent(services).run(_steps("lookup"))["execution_log"]
        aggregated = ResultAggregatorComponent().run(log, "q")["aggregated"]
        commands = CommandBuilderComponent()._build_commands(aggregated)

        assert [c["type"] for c in commands] == ["APPLY_FILTER", "ZOOM_TO_EXTENT"]
        assert commands[0]["payload"]["label"] == "Lindenweg 1"
        min_x, min_y, max_x, max_y = commands[1]["payload"]["bbox"]
        assert min_x < 10.0 < max_x and min_y < 53.6 < max_y  # a point, widened

    def test_no_number_no_map_change(self):
        _, _, commands = _run(DatasetAnswer(operation="count", status="too_many", cap=10000))

        assert commands == []


class TestReply:
    @pytest.mark.parametrize("language, expected", [
        ("de", "3 Gebäude in Nordviertel mit Eigentümer = Wohnbau."),
        ("en", "3 buildings in Nordviertel with owner = Wohnbau."),
    ])
    def test_fixed_template_without_the_llm(self, tmp_path, language, expected):
        class NoLlm:
            def generate(self, *args, **kwargs):
                raise AssertionError("tier 1 numbers must not go through the reply LLM")

        _, aggregated, commands = _run(_count_answer())
        reply = TextResponseComponent(buildings_profile(tmp_path), llm=NoLlm()).run(
            aggregated, "q", commands=commands, language=language
        )["response_text"]

        assert reply == expected
