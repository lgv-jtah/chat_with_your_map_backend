"""``ResultAggregatorComponent``: combines sub-agent results.

Combines the results from all sub-agent calls (location, layer, action)
into a single unified result set, shared by ``CommandBuilderComponent``
(builds the typed ``MapCommand`` stream) and ``TextResponseComponent``
(builds the NLG reply).
"""

from haystack import component


@component
class ResultAggregatorComponent:
    @component.output_types(aggregated=dict)
    def run(self, execution_log: list, query: str):
        by_agent = {entry["agent_name"]: entry["result"] for entry in execution_log}

        aggregated = {
            "query": query,
            "locations": by_agent.get("location_extractor", []),
            "layer": by_agent.get("layer_resolver"),
            "action": by_agent.get("action_detector"),
        }

        return {"aggregated": aggregated}
