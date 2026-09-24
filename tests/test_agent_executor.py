"""Unit tests for ``AgentExecutorComponent``'s sub-agent dispatch,
in particular that ``allowed_layer_ids`` (Section 8 portal scoping) is
forwarded only to the ``layer_resolver`` sub-agent, not to
``location_extractor``/``action_detector`` which have no use for it,
and that ``filter_agent`` sees the same plan's already-resolved
layer/location results.
"""

from cwym_agent.components.agent_executor import AgentExecutorComponent
from cwym_agent.geodata.types import ActionMatch, FilterResult, LayerMatch, LocationMatch


class FakeDataServices:
    def __init__(self):
        self.resolve_layer_calls: list = []
        self.resolve_locations_calls: list = []
        self.detect_action_calls: list = []
        self.apply_filter_calls: list = []

    def resolve_locations(self, query: str):
        self.resolve_locations_calls.append(query)
        return [LocationMatch("Hamburg", 53.5511, 9.9937, 0.9)]

    def resolve_layer(self, query: str, allowed_layer_ids=None):
        self.resolve_layer_calls.append((query, allowed_layer_ids))
        return LayerMatch("Some Layer", "1", 0.9)

    def detect_action(self, query: str):
        self.detect_action_calls.append(query)
        return ActionMatch("Zeigen", 0.85)

    def apply_filter(self, query: str, layer, location=None):
        self.apply_filter_calls.append((query, layer, location))
        return FilterResult(layer_id="1", features={"type": "FeatureCollection", "features": []}, number_matched=0)


def _plan(*agent_names: str) -> dict:
    return {"steps": [{"agent_name": name, "args": {"query": "test"}} for name in agent_names]}


class FakeBridge:
    def __init__(self):
        self.tool_calls: list = []

    def tool_call(self, name, args, result):
        self.tool_calls.append((name, args, result))


class TestAllowedLayerIdsDispatch:
    def test_forwarded_to_layer_resolver_only(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(
            _plan("location_extractor", "layer_resolver", "action_detector"),
            allowed_layer_ids=["27764", "18105"],
        )

        assert data_services.resolve_layer_calls == [("test", ["27764", "18105"])]
        assert data_services.resolve_locations_calls == ["test"]
        assert data_services.detect_action_calls == ["test"]

    def test_defaults_to_none_when_not_given(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(_plan("layer_resolver"))

        assert data_services.resolve_layer_calls == [("test", None)]

    def test_execution_log_unaffected(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        result = component.run(_plan("layer_resolver"), allowed_layer_ids=["1"])

        assert result["execution_log"] == [
            {"agent_name": "layer_resolver", "result": LayerMatch("Some Layer", "1", 0.9)}
        ]


class TestFilterAgentDispatch:
    def test_sees_earlier_steps_layer_and_location(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(_plan("location_extractor", "layer_resolver", "filter_agent"))

        [(query, layer, location)] = data_services.apply_filter_calls
        assert query == "test"
        assert layer == LayerMatch("Some Layer", "1", 0.9)
        assert location == LocationMatch("Hamburg", 53.5511, 9.9937, 0.9)

    def test_no_layer_or_location_step_means_both_none(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(_plan("filter_agent"))

        assert data_services.apply_filter_calls == [("test", None, None)]

    def test_no_location_step_means_location_none(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(_plan("layer_resolver", "filter_agent"))

        [(_, layer, location)] = data_services.apply_filter_calls
        assert layer == LayerMatch("Some Layer", "1", 0.9)
        assert location is None


class TestFilterAgentToolCallResultView:
    def test_bridge_event_carries_feature_count_not_features(self):
        """filter_agent's own TOOL_CALL_RESULT must not repeat the full
        FeatureCollection APPLY_FILTER already carries (CommandBuilderComponent
        builds that from execution_log separately) - the bridge-emitted
        view gets a feature_count instead."""

        class DataServicesWithFeatures(FakeDataServices):
            def apply_filter(self, query, layer, location=None):
                return FilterResult(
                    layer_id="1",
                    features={
                        "type": "FeatureCollection",
                        "features": [{"type": "Feature", "properties": {}} for _ in range(3)],
                    },
                    number_matched=3,
                )

        data_services = DataServicesWithFeatures()
        bridge = FakeBridge()
        component = AgentExecutorComponent(data_services=data_services)

        result = component.run(_plan("filter_agent"), bridge=bridge)

        [(name, _args, emitted)] = bridge.tool_calls
        assert name == "filter_agent"
        assert "features" not in emitted
        assert emitted["feature_count"] == 3
        assert emitted["number_matched"] == 3

        # execution_log keeps the untrimmed result - ResultAggregatorComponent/
        # CommandBuilderComponent need the real features to build APPLY_FILTER.
        logged_result = result["execution_log"][0]["result"]
        assert len(logged_result.features["features"]) == 3

    def test_other_sub_agents_unaffected(self):
        data_services = FakeDataServices()
        bridge = FakeBridge()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(_plan("layer_resolver"), bridge=bridge)

        [(name, _args, emitted)] = bridge.tool_calls
        assert name == "layer_resolver"
        assert emitted == {"name": "Some Layer", "layer_id": "1", "confidence": 0.9, "description": "",
                            "typ": None, "url": None, "collection": None}
