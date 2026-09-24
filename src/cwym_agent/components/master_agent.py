"""``MasterAgentComponent``: query planning.

Analyzes the (German) user query and produces an execution plan deciding
which sub-agents ``AgentExecutorComponent`` should call. Uses the Qwen3
reasoning trace during planning, streamed into the AG-UI bridge as a
``REASONING_*`` event sequence when a bridge is provided.

When conversation context (cmym-agent-architecture.md Section 10) is
available, the current map state / recent history is rendered into a
German context block and prepended to the query text handed to
``Qwen3Client.plan()`` - ``plan()`` places its ``query`` argument
verbatim as the user-role turn, with tool definitions living in its own
separate system prompt, so this only enriches what the planner sees of
*this turn*, without touching that system prompt at all. A follow-up
like "Kannst du mir nur die Gymnasien zeigen?" only has a chance of
resolving correctly if the planner can see that a Schulen layer is
already active.
"""

from typing import Any

from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.geodata.conversation_context import (
    PLANNING_RULES_BLOCK,
    ConversationContext,
    render_context_block,
)
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.llm.qwen3_client import Qwen3Client


@component
class MasterAgentComponent:
    def __init__(self, llm: Any | None = None):
        self.llm = llm if llm is not None else Qwen3Client()

    @component.output_types(plan_json=dict)
    def run(
        self,
        query: str,
        context: ConversationContext | None = None,
        layer_catalog: LayerCatalog | None = None,
        bridge: AGUIEventBridge | None = None,
    ):
        prompt = self._build_planning_prompt(query, context, layer_catalog)

        if bridge:
            reasoning_tokens: list[str] = []
            plan = self.llm.plan(
                prompt,
                streaming_callback=lambda chunk: reasoning_tokens.append(chunk.content),
            )
            bridge.reasoning("".join(reasoning_tokens))
        else:
            plan = self.llm.plan(prompt)

        return {"plan_json": plan}

    @staticmethod
    def _build_planning_prompt(
        query: str, context: ConversationContext | None, layer_catalog: LayerCatalog | None
    ) -> str:
        if context is None:
            return query

        context_block = render_context_block(context, layer_catalog)
        if not context_block:
            # First turn, or a follow-up with genuinely nothing to carry
            # forward - keep the prompt exactly what it was before this
            # feature existed.
            return query

        return f'{context_block}\n\n{PLANNING_RULES_BLOCK}\nNutzeranfrage: "{query}"'
