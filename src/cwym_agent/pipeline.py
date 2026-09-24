"""Assembles the CWYM-Agent Haystack pipeline.

Wires ``MasterAgentComponent`` -> ``AgentExecutorComponent`` ->
``ResultAggregatorComponent`` -> ``CommandBuilderComponent`` ->
``TextResponseComponent`` together, and exposes a single ``run`` entry
point that also forwards the ``query`` and ``bridge`` inputs to every
component that needs them (matching how Haystack allows top-level
unconnected inputs to be supplied per component in
``pipeline.run(data=...)``).

``TextResponseComponent`` runs *after* ``CommandBuilderComponent``, not
in parallel from the same aggregated result - it also takes
``CommandBuilderComponent``'s ``commands`` output as an input, so the
reply it narrates is grounded in what was actually built, not just in
what sub-agents resolved (see ``TextResponseComponent``'s own
docstring for the live bug this fixes).
"""

from typing import Any

from haystack import Pipeline

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.components.agent_executor import AgentExecutorComponent
from cwym_agent.components.command_builder import CommandBuilderComponent
from cwym_agent.components.master_agent import MasterAgentComponent
from cwym_agent.components.result_aggregator import ResultAggregatorComponent
from cwym_agent.components.text_response import TextResponseComponent
from cwym_agent.geodata.conversation_context import ConversationContext
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.llm.qwen3_client import Qwen3Client
from cwym_agent.geodata.real_data_services import RealDataServices


def build_pipeline(llm: Any | None = None, data_services: Any | None = None) -> Pipeline:
    """Build the pipeline, wired to the real Qwen3 + geodata backends by default.

    ``llm``/``data_services`` can be overridden (e.g. with
    ``StubQwen3Generator``/``StubDataServices``) for fast, deterministic
    tests that don't depend on network access or the real vLLM server.

    If ``data_services`` exposes a ``warm_up()`` method (the real
    ``RealDataServices`` does - it fetches and embeds Hamburg's ~6,500
    map layers), it is called here so the cost is paid once, when the
    pipeline is built, not on a user's first query.
    """

    llm = llm if llm is not None else Qwen3Client()
    data_services = data_services if data_services is not None else RealDataServices(llm=llm)

    warm_up = getattr(data_services, "warm_up", None)
    if callable(warm_up):
        warm_up()

    pipeline = Pipeline()

    pipeline.add_component("master_agent", MasterAgentComponent(llm=llm))
    pipeline.add_component("agent_executor", AgentExecutorComponent(data_services=data_services))
    pipeline.add_component("result_aggregator", ResultAggregatorComponent())
    pipeline.add_component("command_builder", CommandBuilderComponent())
    pipeline.add_component("text_response", TextResponseComponent(llm=llm))

    pipeline.connect("master_agent.plan_json", "agent_executor.plan_json")
    pipeline.connect("agent_executor.execution_log", "result_aggregator.execution_log")
    pipeline.connect("result_aggregator.aggregated", "command_builder.aggregated")
    pipeline.connect("result_aggregator.aggregated", "text_response.aggregated")
    pipeline.connect("command_builder.commands", "text_response.commands")

    return pipeline


def run_pipeline(
    pipeline: Pipeline,
    query: str,
    bridge: AGUIEventBridge | None = None,
    allowed_layer_ids: list[str] | None = None,
    context: ConversationContext | None = None,
    layer_catalog: LayerCatalog | None = None,
) -> str:
    """Run the pipeline for one query, returning the final response text.

    :param allowed_layer_ids: the requesting portal's own layer ids
        (``PortalCatalogResolver``, Section 8), or ``None`` to search
        Hamburg's full layer catalog unscoped.
    :param context: this request's validated conversation context
        (Section 10) - map state + recent history. ``None`` on a
        request with no usable ``portal_context``/state to validate
        against.
    :param layer_catalog: the same portal catalog ``allowed_layer_ids``
        was derived from, threaded separately because sub-agents/NLG
        need layer *metadata* (names, an active layer's typ/url for
        FilterAgent targeting), not just the bare id list.
    """

    result = pipeline.run(
        data={
            "master_agent": {"query": query, "context": context, "layer_catalog": layer_catalog, "bridge": bridge},
            "agent_executor": {
                "bridge": bridge,
                "allowed_layer_ids": allowed_layer_ids,
                "context": context,
                "layer_catalog": layer_catalog,
            },
            "result_aggregator": {"query": query},
            "command_builder": {"context": context, "bridge": bridge},
            "text_response": {"query": query, "context": context, "layer_catalog": layer_catalog, "bridge": bridge},
        }
    )

    return result["text_response"]["response_text"]
