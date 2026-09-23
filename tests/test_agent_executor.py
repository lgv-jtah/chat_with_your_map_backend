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
