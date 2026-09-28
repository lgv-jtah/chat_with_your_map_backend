"""``AgentExecutorComponent``: runs the sub-agents from the execution plan.

Orchestrates the specialized sub-agents (location extractor, layer
resolver, action detector, filter agent) dynamically according to the
plan produced by ``MasterAgentComponent``, possibly with multiple calls
per query. Each sub-agent call is reported to the AG-UI bridge as a
``TOOL_CALL_*``/``TOOL_CALL_RESULT`` event sequence when a bridge is
provided.
"""

from dataclasses import asdict, is_dataclass
from typing import Any

from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.geodata.conversation_context import ConversationContext
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.real_data_services import RealDataServices
from cwym_agent.geodata.types import FilterCondition


def _serializable(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_serializable(item) for item in value]
    return value


def _tool_call_result_view(agent_name: str, serialized_result):
    """The value sent as filter_agent's own TOOL_CALL_RESULT.

    filter_agent's full result (including the complete GeoJSON
    FeatureCollection) is also what CommandBuilderComponent pushes as
    the APPLY_FILTER MapCommand right after - sending the whole
    FeatureCollection twice on the wire is pure duplication for large
    result sets, so this view carries a feature count instead. Only
    affects what's emitted on the bridge; execution_log/results_by_agent
    keep the untrimmed result, since ResultAggregatorComponent/
    TextResponseComponent/CommandBuilderComponent still need it.
    """

    if agent_name == "filter_agent" and isinstance(serialized_result, dict) and "features" in serialized_result:
        features = serialized_result["features"] or {}
        return {
            **{key: value for key, value in serialized_result.items() if key != "features"},
            "feature_count": len(features.get("features", [])),
        }
    return serialized_result


@component
class AgentExecutorComponent:
    def __init__(self, data_services: Any | None = None):
        self.data_services = data_services if data_services is not None else RealDataServices()

    @component.output_types(execution_log=list)
    def run(
        self,
        plan_json: dict,
        bridge: AGUIEventBridge | None = None,
        allowed_layer_ids: list[str] | None = None,
        context: ConversationContext | None = None,
        layer_catalog: LayerCatalog | None = None,
    ):
        execution_log = []
        # Accumulated as steps execute (not just aggregated afterward),
        # so a later step in the same plan - filter_agent - can see an
        # earlier step's result, e.g. the layer/location it should
        # filter. Relies on MasterAgentComponent's plan ordering
        # location_extractor/layer_resolver before filter_agent (see
        # sub_agents.py's filter_agent description) - a best-effort
        # prompt-engineering guarantee like the rest of this pipeline's
        # LLM-assisted steps, not a hard one.
        results_by_agent: dict[str, object] = {}

        for step in plan_json.get("steps", []):
            agent_name = step["agent_name"]
            args = step.get("args", {})
            result = self._run_sub_agent(
                agent_name, args, allowed_layer_ids, results_by_agent, context, layer_catalog
            )
            results_by_agent[agent_name] = result

            if bridge:
                bridge.tool_call(
                    agent_name, args, _tool_call_result_view(agent_name, _serializable(result))
                )

            execution_log.append({"agent_name": agent_name, "result": result})

        return {"execution_log": execution_log}

    def _run_sub_agent(
        self,
        agent_name: str,
        args: dict,
        allowed_layer_ids: list[str] | None,
        results_by_agent: dict[str, object],
        context: ConversationContext | None,
        layer_catalog: LayerCatalog | None,
    ):
        query = args.get("query", "")

        if agent_name == "location_extractor":
            return self.data_services.resolve_locations(query)
        if agent_name == "layer_resolver":
            return self.data_services.resolve_layer(query, allowed_layer_ids=allowed_layer_ids)
        if agent_name == "action_detector":
            action = self.data_services.detect_action(query)
            # The planner already sees active layer names+ids in its own
            # prompt (Section 10.5) and matches them itself when the
            # query names one to stop showing - passed straight through
            # as a plan arg, not re-resolved here. command_builder
            # validates these against what's actually active before
            # building any DEACTIVATE_LAYER command.
            action.deactivate_layer_ids = [str(i) for i in args.get("deactivateLayerIds") or []]
            return action
        if agent_name == "filter_agent":
            return self._run_filter_agent(query, args, results_by_agent, context, layer_catalog)

        raise ValueError(f"Unknown sub-agent: {agent_name}")

    def _run_filter_agent(
        self,
        query: str,
        args: dict,
        results_by_agent: dict[str, object],
        context: ConversationContext | None,
        layer_catalog: LayerCatalog | None,
    ):
        # A follow-up naming an already-active layer (Section 10.5's
        # planning rule 2) doesn't call layer_resolver at all - the plan
        # carries the target layerId directly, resolved to metadata via
        # LayerCatalog instead of a fresh vector search.
        layer = results_by_agent.get("layer_resolver")
        target_layer_id = args.get("layerId")
        if layer is None and target_layer_id and layer_catalog is not None:
            layer = layer_catalog.get(target_layer_id)

        locations = results_by_agent.get("location_extractor") or []
        location = locations[0] if locations else None

        mode = args.get("mode", "replace")
        prior_conditions = None
        if mode == "refine" and context is not None:
            effective_layer_id = target_layer_id or (layer.layer_id if layer else None)
            filter_state = next(
                (f for f in context.map_state.filters if f.layer_id == effective_layer_id), None
            )
            if filter_state is not None:
                prior_conditions = [
                    FilterCondition(field=c.field, operator=c.operator, value=c.value)
                    for c in filter_state.filter_spec.conditions
                ]

        return self.data_services.apply_filter(
            query, layer, location, mode=mode, prior_conditions=prior_conditions
        )
