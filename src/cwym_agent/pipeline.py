"""Assembles the CWYM-Agent Haystack pipeline.

Wires ``MasterAgentComponent`` -> ``AgentExecutorComponent`` ->
``ResultAggregatorComponent`` -> ``TextResponseComponent`` together, and
exposes a single ``run`` entry point that also forwards the ``query`` and
``bridge`` inputs to every component that needs them (matching how
Haystack allows top-level unconnected inputs to be supplied per
component in ``pipeline.run(data=...)``).
"""

from haystack import Pipeline

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.components.agent_executor import AgentExecutorComponent
from cwym_agent.components.master_agent import MasterAgentComponent
from cwym_agent.components.result_aggregator import ResultAggregatorComponent
from cwym_agent.components.text_response import TextResponseComponent


def build_pipeline() -> Pipeline:
    pipeline = Pipeline()

    pipeline.add_component("master_agent", MasterAgentComponent())
    pipeline.add_component("agent_executor", AgentExecutorComponent())
    pipeline.add_component("result_aggregator", ResultAggregatorComponent())
    pipeline.add_component("text_response", TextResponseComponent())

    pipeline.connect("master_agent.plan_json", "agent_executor.plan_json")
    pipeline.connect("agent_executor.execution_log", "result_aggregator.execution_log")
    pipeline.connect("result_aggregator.aggregated", "text_response.aggregated")

    return pipeline


def run_pipeline(pipeline: Pipeline, query: str, bridge: AGUIEventBridge | None = None) -> str:
    """Run the pipeline for one query, returning the final response text."""

    result = pipeline.run(
        data={
            "master_agent": {"query": query, "bridge": bridge},
            "agent_executor": {"bridge": bridge},
            "result_aggregator": {"query": query, "bridge": bridge},
            "text_response": {"query": query, "bridge": bridge},
        }
    )

    return result["text_response"]["response_text"]
