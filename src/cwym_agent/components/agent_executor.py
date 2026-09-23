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
from cwym_agent.geodata.real_data_services import RealDataServices


def _serializable(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_serializable(item) for item in value]
    return value


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
            result = self._run_sub_agent(agent_name, args, allowed_layer_ids, results_by_agent)
            results_by_agent[agent_name] = result

            if bridge:
                bridge.tool_call(agent_name, args, _serializable(result))

            execution_log.append({"agent_name": agent_name, "result": result})

        return {"execution_log": execution_log}

    def _run_sub_agent(
        self,
        agent_name: str,
        args: dict,
        allowed_layer_ids: list[str] | None,
        results_by_agent: dict[str, object],
    ):
        query = args.get("query", "")

        if agent_name == "location_extractor":
            return self.data_services.resolve_locations(query)
        if agent_name == "layer_resolver":
            return self.data_services.resolve_layer(query, allowed_layer_ids=allowed_layer_ids)
        if agent_name == "action_detector":
            return self.data_services.detect_action(query)
        if agent_name == "filter_agent":
            layer = results_by_agent.get("layer_resolver")
            locations = results_by_agent.get("location_extractor") or []
            location = locations[0] if locations else None
            return self.data_services.apply_filter(query, layer, location)

        raise ValueError(f"Unknown sub-agent: {agent_name}")
