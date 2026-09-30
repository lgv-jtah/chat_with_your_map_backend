"""Unit tests for ``AgentExecutorComponent``'s sub-agent dispatch,
in particular that ``allowed_layer_ids`` (Section 8 portal scoping) is
forwarded only to the ``layer_resolver`` sub-agent, not to
``location_extractor``/``action_detector`` which have no use for it,
and that ``filter_agent`` sees the same plan's already-resolved
layer/location results.
"""

from ag_ui.core import RunAgentInput, UserMessage

from cwym_agent.components.agent_executor import AgentExecutorComponent
from cwym_agent.geodata.conversation_context import ConversationContext, MapStateIn, build_context
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.types import (
    ActionMatch,
    AnalysisResult,
    FilterCondition,
    FilterResult,
    LayerMatch,
    LocationMatch,
    ResultLayer,
)


class FakeDataServices:
    def __init__(self):
        self.resolve_layer_calls: list = []
        self.resolve_locations_calls: list = []
        self.detect_action_calls: list = []
        self.apply_filter_calls: list = []
        self.analyze_calls: list = []

    def resolve_locations(self, query: str):
        self.resolve_locations_calls.append(query)
        return [LocationMatch("Hamburg", 53.5511, 9.9937, 0.9)]

    def resolve_layer(self, query: str, allowed_layer_ids=None):
        self.resolve_layer_calls.append((query, allowed_layer_ids))
        return LayerMatch("Some Layer", "1", 0.9)

    def detect_action(self, query: str):
        self.detect_action_calls.append(query)
        return ActionMatch("Zeigen", 0.85)

    def analyze(self, query: str, history=None):
        self.analyze_calls.append((query, history))
        return AnalysisResult(
            answer="19 Stationen.",
            layers=[
                ResultLayer(
                    key="stations",
                    label="Stationen",
                    role="result",
                    features={"type": "FeatureCollection", "features": [{"type": "Feature"}]},
                    feature_count=1,
                    total_features=1,
                )
            ],
        )

    def apply_filter(self, query: str, layer, location=None, mode="replace", prior_conditions=None):
        self.apply_filter_calls.append((query, layer, location, mode, prior_conditions))
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


class TestActionDetectorDeactivateLayerIds:
    """"zeige U-Bahn, aber nicht mehr Fahrradstationen" (Section 10.5
    rule 5) - the planner matches active layer names against the query
    itself and passes the id(s) straight through as a plan arg."""

    def test_deactivate_layer_ids_attached_to_action_match(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)
        plan = {
            "steps": [
                {
                    "agent_name": "action_detector",
                    "args": {"query": "test", "deactivateLayerIds": ["27764"]},
                }
            ]
        }

        result = component.run(plan)

        [entry] = result["execution_log"]
        assert entry["result"] == ActionMatch("Zeigen", 0.85, ["27764"])

    def test_missing_deactivate_layer_ids_defaults_to_empty(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        result = component.run(_plan("action_detector"))

        [entry] = result["execution_log"]
        assert entry["result"].deactivate_layer_ids == []


class TestActionDetectorClearFilterLayerIds:
    """"zeig wieder alle" (Section 10.5 rule 6) - same pass-through as
    deactivateLayerIds."""

    def test_clear_filter_layer_ids_attached_to_action_match(self):
        component = AgentExecutorComponent(data_services=FakeDataServices())
        plan = {
            "steps": [
                {"agent_name": "action_detector", "args": {"query": "test", "clearFilterLayerIds": [1732]}}
            ]
        }

        [entry] = component.run(plan)["execution_log"]

        assert entry["result"].clear_filter_layer_ids == ["1732"]

    def test_missing_clear_filter_layer_ids_defaults_to_empty(self):
        component = AgentExecutorComponent(data_services=FakeDataServices())

        [entry] = component.run(_plan("action_detector"))["execution_log"]

        assert entry["result"].clear_filter_layer_ids == []


class TestFilterAgentDispatch:
    def test_sees_earlier_steps_layer_and_location(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(_plan("location_extractor", "layer_resolver", "filter_agent"))

        [(query, layer, location, mode, prior_conditions)] = data_services.apply_filter_calls
        assert query == "test"
        assert layer == LayerMatch("Some Layer", "1", 0.9)
        assert location == LocationMatch("Hamburg", 53.5511, 9.9937, 0.9)
        assert mode == "replace"
        assert prior_conditions is None

    def test_filter_agent_planned_first_still_sees_layer_and_location(self):
        """Found live: Qwen3 often plans filter_agent before the steps it
        depends on - the executor runs it last regardless."""

        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        result = component.run(_plan("filter_agent", "layer_resolver", "location_extractor"))

        [(_, layer, location, _, _)] = data_services.apply_filter_calls
        assert layer == LayerMatch("Some Layer", "1", 0.9)
        assert location == LocationMatch("Hamburg", 53.5511, 9.9937, 0.9)
        assert [e["agent_name"] for e in result["execution_log"]] == [
            "layer_resolver",
            "location_extractor",
            "filter_agent",
        ]

    def test_non_filter_steps_keep_their_planned_order(self):
        component = AgentExecutorComponent(data_services=FakeDataServices())

        result = component.run(
            _plan("action_detector", "filter_agent", "location_extractor", "layer_resolver")
        )

        assert [e["agent_name"] for e in result["execution_log"]] == [
            "action_detector",
            "location_extractor",
            "layer_resolver",
            "filter_agent",
        ]

    def test_no_layer_or_location_step_means_both_none(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(_plan("filter_agent"))

        assert data_services.apply_filter_calls == [("test", None, None, "replace", None)]

    def test_no_location_step_means_location_none(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)

        component.run(_plan("layer_resolver", "filter_agent"))

        [(_, layer, location, _mode, _prior)] = data_services.apply_filter_calls
        assert layer == LayerMatch("Some Layer", "1", 0.9)
        assert location is None

    def test_layer_id_arg_targets_catalog_layer_without_layer_resolver_step(self):
        """Planning rule 2 (Section 10.5): a follow-up filtering an
        already-active layer plans filter_agent with a layerId arg and
        no layer_resolver step at all."""

        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)
        catalog = LayerCatalog({"1732": LayerMatch("Schulen Hamburg", "1732", 1.0, typ="OAF")})
        plan = {"steps": [{"agent_name": "filter_agent", "args": {"query": "test", "layerId": "1732"}}]}

        component.run(plan, layer_catalog=catalog)

        [(_, layer, _loc, _mode, _prior)] = data_services.apply_filter_calls
        assert layer == LayerMatch("Schulen Hamburg", "1732", 1.0, typ="OAF")

    def test_refine_mode_looks_up_prior_conditions_for_target_layer(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)
        catalog = LayerCatalog({"1732": LayerMatch("Schulen Hamburg", "1732", 1.0, typ="OAF")})
        context = build_context(
            RunAgentInput(
                thread_id="t",
                run_id="r",
                state={
                    "filters": [
                        {
                            "layerId": "1732",
                            "filterSpec": {
                                "conditions": [{"field": "schulform", "operator": "eq", "value": "Grundschule"}]
                            },
                        }
                    ]
                },
                messages=[UserMessage(id="1", content="nur Gymnasien")],
                tools=[],
                context=[],
                forwarded_props={},
            ),
            catalog,
        )
        plan = {
            "steps": [
                {"agent_name": "filter_agent", "args": {"query": "nur Gymnasien", "layerId": "1732", "mode": "refine"}}
            ]
        }

        component.run(plan, context=context, layer_catalog=catalog)

        [(_, _layer, _loc, mode, prior)] = data_services.apply_filter_calls
        assert mode == "refine"
        assert prior == [FilterCondition(field="schulform", operator="eq", value="Grundschule")]


class TestFilterAgentToolCallResultView:
    def test_bridge_event_carries_feature_count_not_features(self):
        """filter_agent's own TOOL_CALL_RESULT must not repeat the full
        FeatureCollection APPLY_FILTER already carries (CommandBuilderComponent
        builds that from execution_log separately) - the bridge-emitted
        view gets a feature_count instead."""

        class DataServicesWithFeatures(FakeDataServices):
            def apply_filter(self, query, layer, location=None, mode="replace", prior_conditions=None):
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


class TestUdpAnalysisDispatch:
    def test_passes_query_and_conversation_history(self):
        data_services = FakeDataServices()
        component = AgentExecutorComponent(data_services=data_services)
        context = ConversationContext(
            map_state=MapStateIn(), history=[("user", "Wie viele?"), ("assistant", "Welche meinen Sie?")]
        )

        component.run(_plan("udp_analysis"), context=context)

        assert data_services.analyze_calls == [
            ("test", [("user", "Wie viele?"), ("assistant", "Welche meinen Sie?")])
        ]

    def test_no_context_means_empty_history(self):
        data_services = FakeDataServices()

        AgentExecutorComponent(data_services=data_services).run(_plan("udp_analysis"))

        assert data_services.analyze_calls == [("test", [])]

    def test_tool_call_result_leaves_out_layer_features(self):
        bridge = FakeBridge()

        AgentExecutorComponent(data_services=FakeDataServices()).run(_plan("udp_analysis"), bridge=bridge)

        [(name, _, result)] = bridge.tool_calls
        assert name == "udp_analysis"
        assert result["answer"] == "19 Stationen."
        assert result["layers"][0]["feature_count"] == 1
        assert "features" not in result["layers"][0]
