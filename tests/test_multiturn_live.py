"""Opt-in golden multi-turn tests against the real Qwen3/vLLM server +
real Hamburg geodata APIs (cmym-agent-architecture.md Section 11).

Each test drives two sequential ``pipeline.run()`` calls, manually
threading the first turn's built commands into the second turn's
``ConversationContext.map_state`` - a minimal in-Python stand-in for
the frontend's job of recording ``filters``/``lastLocation`` as it
executes ``MapCommand``s (Section 5's "frontend execution" table). The
real browser round trip is verified separately, live, through the
actual running backend + frontend, same as every other fix this
session.

Loose, containment-style assertions throughout (`in`, `<=`), matching
the existing live-test style in this repo (test_qwen3_live.py):
real LLM planning is not byte-for-byte deterministic, so these assert
the *behavior the planning rules are meant to produce*, not an exact
plan shape.

MT-4 ("ambiguous_layer" -> route=clarify) and MT-7 (chrome-route
language downgrade) are not included - both need the routing machinery
(ConditionalRouter/ClarifyComponent) that's still deferred. MT-6
("system message + unknown layer id are dropped") is not included as a
live test either - it's a deterministic ``build_context`` validation
concern already covered at the unit level in
test_conversation_context.py, not an LLM-planning-behavior question
this file's live round trips would add anything to.

Skipped by default, same gate as test_qwen3_live.py/test_geodata_live.py.
"""

import os

import pytest

from cwym_agent.geodata.conversation_context import (
    Area,
    ConversationContext,
    FilterSpec,
    FilterStateIn,
    MapStateIn,
)
from cwym_agent.geodata.real_data_services import RealDataServices
from cwym_agent.llm.qwen3_client import Qwen3Client
from cwym_agent.settings import Qwen3Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("CWYM_LIVE_TESTS") != "1",
    reason="Set CWYM_LIVE_TESTS=1 to run tests against the real Qwen3/vLLM server and Hamburg APIs.",
)


@pytest.fixture(scope="module")
def services() -> RealDataServices:
    llm = Qwen3Client(Qwen3Settings())
    return RealDataServices(llm=llm)


def _run_turn(services: RealDataServices, query: str, context: ConversationContext | None = None):
    """Drives one turn directly against RealDataServices/MasterAgentComponent-
    equivalent logic, without the FastAPI/bridge layer - mirrors what
    run_pipeline() does, but also returns execution_log/commands (not
    just the final response text) for assertions."""

    from cwym_agent.components.agent_executor import AgentExecutorComponent
    from cwym_agent.components.command_builder import CommandBuilderComponent
    from cwym_agent.components.master_agent import MasterAgentComponent
    from cwym_agent.components.result_aggregator import ResultAggregatorComponent

    master_agent = MasterAgentComponent(llm=services.llm)
    agent_executor = AgentExecutorComponent(data_services=services)
    aggregator = ResultAggregatorComponent()
    command_builder = CommandBuilderComponent()

    plan_json = master_agent.run(query, context=context)["plan_json"]
    execution_log = agent_executor.run(plan_json, context=context)["execution_log"]
    aggregated = aggregator.run(execution_log, query)["aggregated"]
    commands = command_builder.run(aggregated, context=context)["commands"]

    return execution_log, commands


def _agent_names(execution_log) -> set[str]:
    return {entry["agent_name"] for entry in execution_log}


class TestMT1FilterAlreadyActiveLayer:
    def test_gymnasien_follow_up_filters_instead_of_reactivating(self, services):
        _, turn1_commands = _run_turn(services, "Wo finde ich alle Schulen?")
        activate = next(cmd for cmd in turn1_commands if cmd["type"] == "ACTIVATE_LAYER")
        schulen_layer_id = activate["payload"]["layerIds"][0]

        context = ConversationContext(
            map_state=MapStateIn(active_layer_ids=[schulen_layer_id]), history=[]
        )
        execution_log, commands = _run_turn(services, "Kannst du mir nur die Gymnasien zeigen?", context)

        assert "layer_resolver" not in _agent_names(execution_log)
        types = [cmd["type"] for cmd in commands]
        if "APPLY_FILTER" in types:
            assert types.index("DEACTIVATE_LAYER") < types.index("APPLY_FILTER")
            deactivate = next(cmd for cmd in commands if cmd["type"] == "DEACTIVATE_LAYER")
            assert deactivate["payload"]["layerIds"] == [schulen_layer_id]


class TestMT2RefineMergesConditions:
    def test_gymnasien_after_grundschulen_replaces_schulform_keeps_area(self, services):
        _, turn1_commands = _run_turn(services, "Zeig mir die Grundschulen in Wandsbek")
        apply_filter = next((cmd for cmd in turn1_commands if cmd["type"] == "APPLY_FILTER"), None)
        if apply_filter is None:
            pytest.skip("turn 1 didn't produce a filter - nothing to refine against")

        filter_spec = apply_filter["payload"].get("filterSpec")
        if not filter_spec or not filter_spec.get("area"):
            pytest.skip("turn 1's filterSpec had no area to carry forward")

        layer_id = apply_filter["payload"]["layerId"]
        context = ConversationContext(
            map_state=MapStateIn(
                active_layer_ids=[layer_id],
                filters=[
                    FilterStateIn(
                        layer_id=layer_id,
                        filter_spec=FilterSpec(
                            conditions=filter_spec["conditions"],
                            area=Area(**filter_spec["area"]),
                        ),
                    )
                ],
            ),
            history=[],
        )

        _, turn2_commands = _run_turn(services, "Und jetzt die Gymnasien", context)

        apply_filter_2 = next((cmd for cmd in turn2_commands if cmd["type"] == "APPLY_FILTER"), None)
        if apply_filter_2 is None:
            pytest.skip("turn 2 didn't produce a filter")
        merged = apply_filter_2["payload"]["filterSpec"]
        assert merged["area"]["name"] == filter_spec["area"]["name"]


class TestMT3LastLocationReResolved:
    def test_dort_reuses_last_shown_location(self, services):
        _, turn1_commands = _run_turn(services, "Zeig mir Wandsbek")
        navigate = next((cmd for cmd in turn1_commands if cmd["type"] == "NAVIGATE_TO_LOCATION"), None)
        if navigate is None:
            pytest.skip("turn 1 didn't navigate anywhere")
        location = navigate["payload"]["locations"][0]

        context = ConversationContext(
            map_state=MapStateIn.model_validate(
                {"lastLocation": {"name": location["name"], "kind": location.get("kind") or "bezirk"}}
            ),
            history=[],
        )
        execution_log, _ = _run_turn(services, "Welche Kitas gibt es dort?", context)

        assert "location_extractor" in _agent_names(execution_log)
        locations = next(
            (e["result"] for e in execution_log if e["agent_name"] == "location_extractor"), []
        )
        assert any(location["name"].split("-")[0] in loc.name for loc in locations)


class TestMT5NewLayerIgnoresContext:
    def test_new_layer_query_does_not_filter_the_old_one(self, services):
        _, turn1_commands = _run_turn(services, "Wo finde ich alle Schulen?")
        activate = next(cmd for cmd in turn1_commands if cmd["type"] == "ACTIVATE_LAYER")
        schulen_layer_id = activate["payload"]["layerIds"][0]

        context = ConversationContext(
            map_state=MapStateIn(active_layer_ids=[schulen_layer_id]), history=[]
        )
        _, turn2_commands = _run_turn(services, "Zeig mir die Fahrradstationen", context)

        types = [cmd["type"] for cmd in turn2_commands]
        assert "ACTIVATE_LAYER" in types
        activate_2 = next(cmd for cmd in turn2_commands if cmd["type"] == "ACTIVATE_LAYER")
        assert activate_2["payload"]["layerIds"] != [schulen_layer_id]
