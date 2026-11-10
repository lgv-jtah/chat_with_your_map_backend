"""``ResultAggregatorComponent``: combines sub-agent results.

Combines the results from all sub-agent calls (location, layer, action,
filter, analysis) into a single unified result set, shared by ``CommandBuilderComponent``
(builds the typed ``MapCommand`` stream) and ``TextResponseComponent``
(builds the NLG reply).
"""

from haystack import component

from cwym_agent.geodata.types import DatasetAnswer


@component
class ResultAggregatorComponent:
    @component.output_types(aggregated=dict)
    def run(self, execution_log: list, query: str):
        by_agent = {entry["agent_name"]: entry["result"] for entry in execution_log}
        filter_output = by_agent.get("filter_agent")
        dataset_answer = filter_output if isinstance(filter_output, DatasetAnswer) else None

        aggregated = {
            "query": query,
            "locations": by_agent.get("location_extractor", []),
            "layer": by_agent.get("layer_resolver"),
            "action": by_agent.get("action_detector"),
            "filter_result": None if dataset_answer else filter_output,
            # filter_agent with an operation: a tier 1 analysis (14.4).
            "dataset_answer": dataset_answer,
            "analysis_result": by_agent.get("udp_analysis"),
            # filter_agent ran but produced no result (layer type not
            # filterable, service error, nothing to filter by) - the
            # answer must then not claim a filtered view.
            "filter_failed": "filter_agent" in by_agent and by_agent["filter_agent"] is None,
            # A place the query could mean several areas for ("Hoheluft"):
            # no map commands, the answer asks which one.
            "ambiguous_location": next(
                (loc for loc in by_agent.get("location_extractor") or [] if getattr(loc, "alternatives", None)),
                None,
            ),
        }

        return {"aggregated": aggregated}
