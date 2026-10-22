"""``AgentExecutorComponent``: runs the sub-agents from the execution plan.

Orchestrates the specialized sub-agents (location extractor, layer
resolver, action detector, filter agent) dynamically according to the
plan produced by ``MasterAgentComponent``, possibly with multiple calls
per query. Each sub-agent call is reported to the AG-UI bridge as a
``TOOL_CALL_*``/``TOOL_CALL_RESULT`` event sequence when a bridge is
provided.
"""

import logging
from dataclasses import asdict, is_dataclass, replace
from typing import Any

from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.geodata.conversation_context import ConversationContext
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.real_data_services import RealDataServices
from cwym_agent.geodata.types import FilterCondition, LayerMatch

logger = logging.getLogger(__name__)


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
    # Same for udp_analysis: each layer's FeatureCollection travels once,
    # in its ADD_RESULT_LAYER command.
    if agent_name == "udp_analysis" and isinstance(serialized_result, dict):
        return {
            **serialized_result,
            "layers": [
                {key: value for key, value in layer.items() if key != "features"}
                for layer in serialized_result.get("layers") or []
            ],
        }
    return serialized_result


def _dependencies_first(steps: list[dict]) -> list[dict]:
    """Moves filter_agent steps after every other step, keeping each
    group's own order.

    filter_agent reads layer_resolver's/location_extractor's results,
    but the plan's order is whatever Qwen3 emitted - found live: for
    "Filtere Krankenhäuser in Wandsbek", 5 of 10 plans put filter_agent
    first despite sub_agents.py's description asking otherwise, so it
    ran with no layer and filtered nothing. No other sub-agent reads an
    earlier step's result, so moving filter_agent last is the whole
    dependency order.
    """

    ordered = [s for s in steps if s["agent_name"] != "filter_agent"] + [
        s for s in steps if s["agent_name"] == "filter_agent"
    ]
    if ordered != steps:
        logger.info(
            "agent_executor: reordered plan %r -> %r",
            [s["agent_name"] for s in steps],
            [s["agent_name"] for s in ordered],
        )
    return ordered


def _with_portal_details(layer: LayerMatch, portal_layer: LayerMatch | None) -> LayerMatch:
    """Fills the service details a layer is missing (e.g. the WFS feature
    type) from the portal's own entry for the same id."""

    if portal_layer is None:
        return layer
    details = {
        name: getattr(portal_layer, name)
        for name in ("typ", "url", "collection", "feature_type", "feature_ns")
        if getattr(layer, name) is None and getattr(portal_layer, name) is not None
    }
    return replace(layer, **details) if details else layer


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
        # filter.
        results_by_agent: dict[str, object] = {}

        for step in _dependencies_first(plan_json.get("steps", [])):
            agent_name = step["agent_name"]
            args = step.get("args", {})
            # START goes out before the sub-agent runs, not together with
            # its result afterwards - otherwise the chat can only show a
            # step once it's already done (a UDP analysis: minutes of
            # nothing, then the step appearing complete).
            call_id = bridge.tool_call_start(agent_name, args) if bridge else None
            result = self._run_sub_agent(
                agent_name, args, allowed_layer_ids, results_by_agent, context, layer_catalog
            )
            results_by_agent[agent_name] = result

            if bridge:
                bridge.tool_call_result(
                    call_id, _tool_call_result_view(agent_name, _serializable(result))
                )

            execution_log.append({"agent_name": agent_name, "result": result})

        implicit_query = self._implicit_filter_query(plan_json.get("steps", []), results_by_agent, layer_catalog)
        if implicit_query is not None:
            logger.info("agent_executor: layer + area resolved but no filter planned - filtering %r", implicit_query)
            args = {"query": implicit_query}
            call_id = bridge.tool_call_start("filter_agent", args) if bridge else None
            result = self._run_sub_agent(
                "filter_agent", args, allowed_layer_ids, results_by_agent, context, layer_catalog
            )
            results_by_agent["filter_agent"] = result
            if bridge:
                bridge.tool_call_result(call_id, _tool_call_result_view("filter_agent", _serializable(result)))
            execution_log.append({"agent_name": "filter_agent", "result": result})

        return {"execution_log": execution_log}

    @staticmethod
    def _implicit_filter_query(
        steps: list[dict], results_by_agent: dict[str, object], layer_catalog: LayerCatalog | None
    ) -> str | None:
        """A layer in an area ("Grundschulen in Hoheluft-West") means only
        that area's features - but the planner doesn't always plan
        filter_agent for it: found live, for the same question it did in
        only 2 of 5 runs and otherwise switched on the whole layer. So when
        a filterable layer (OAF, or WFS with a feature type) and an area
        with a boundary are resolved and nothing else decides the turn,
        the filter runs anyway. Returns the filter query, or None."""

        if "filter_agent" in results_by_agent or "udp_analysis" in results_by_agent:
            return None
        layer = results_by_agent.get("layer_resolver")
        locations = results_by_agent.get("location_extractor") or []
        location = locations[0] if locations else None
        if layer is None or location is None or not location.boundary or location.alternatives:
            return None

        action = results_by_agent.get("action_detector")
        if action is not None and (
            action.action_type == "Öffne" or action.deactivate_layer_ids or action.clear_filter_layer_ids
        ):
            return None

        if layer_catalog is not None:
            layer = _with_portal_details(layer, layer_catalog.get(layer.layer_id))
        filterable = (layer.typ == "OAF" and layer.collection and layer.url) or (
            layer.typ == "WFS" and layer.feature_type and layer.url
        )
        if not filterable:
            return None

        layer_query = next(
            (s.get("args", {}).get("query") for s in steps if s.get("agent_name") == "layer_resolver"), None
        )
        return f"{layer_query or layer.name} in {location.name}"

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
            # A layer the portal itself names exactly like the query wins
            # before the central search - which only knows Hamburg's
            # catalog ids and can't find a portal-only variant such as
            # "Grundschulen" (8712.1 = the schools WFS 8712 with a filter).
            portal_match = layer_catalog.match_name(query) if layer_catalog is not None else None
            if portal_match is not None:
                logger.info("agent_executor: %r matched portal layer %s by name", query, portal_match.layer_id)
                return portal_match
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
            action.clear_filter_layer_ids = [str(i) for i in args.get("clearFilterLayerIds") or []]
            return action
        if agent_name == "filter_agent":
            return self._run_filter_agent(query, args, results_by_agent, context, layer_catalog)
        if agent_name == "udp_analysis":
            # The UDP agent keeps no session of its own - the recent chat
            # is what lets it resolve its own clarifying questions.
            return self.data_services.analyze(query, context.history if context else [])

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

        # The central search knows a layer's service type/URL but not a
        # WFS feature type - the portal's own layer list does.
        if layer is not None and layer_catalog is not None:
            layer = _with_portal_details(layer, layer_catalog.get(layer.layer_id))

        locations = results_by_agent.get("location_extractor") or []
        location = locations[0] if locations else None
        if location is not None and location.alternatives:
            # "Hoheluft" = Hoheluft-Ost or -West: filtering one of them
            # would be a guess - the answer asks back instead.
            logger.info("agent_executor: location %r is ambiguous, not filtering", location.name)
            return None

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
