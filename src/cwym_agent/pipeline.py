"""Assembles the CWYM-Agent Haystack pipeline.

Wires ``MasterAgentComponent`` -> ``AgentExecutorComponent`` ->
``ResultAggregatorComponent`` -> ``TextResponseComponent`` together, and
exposes a single ``run`` entry point that also forwards the ``query`` and
``bridge`` inputs to every component that needs them (matching how
Haystack allows top-level unconnected inputs to be supplied per
component in ``pipeline.run(data=...)``).
"""

from typing import Any

from haystack import Pipeline

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.components.agent_executor import AgentExecutorComponent
from cwym_agent.components.master_agent import MasterAgentComponent
from cwym_agent.components.result_aggregator import ResultAggregatorComponent
from cwym_agent.components.text_response import TextResponseComponent
from cwym_agent.llm.qwen3_client import Qwen3Client
from cwym_agent.stubs.data_services import StubDataServices


def build_pipeline(llm: Any | None = None, data_services: Any | None = None) -> Pipeline:
    """Build the pipeline, wired to the real Qwen3 backend by default.

    ``llm``/``data_services`` can be overridden (e.g. with
    ``StubQwen3Generator``/``StubDataServices``) for fast, deterministic
    tests that don't depend on network access to the real vLLM server.
    Data services are still stubbed by default - real vector-search/geo
    API integration is a separate, later phase.
    """

    llm = llm if llm is not None else Qwen3Client()
    data_services = data_services if data_services is not None else StubDataServices()

    pipeline = Pipeline()

    pipeline.add_component("master_agent", MasterAgentComponent(llm=llm))
    pipeline.add_component("agent_executor", AgentExecutorComponent(data_services=data_services))
    pipeline.add_component("result_aggregator", ResultAggregatorComponent())
    pipeline.add_component("text_response", TextResponseComponent(llm=llm))

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
