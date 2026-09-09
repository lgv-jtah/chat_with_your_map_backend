"""``MasterAgentComponent``: query planning.

Analyzes the (German) user query and produces an execution plan deciding
which sub-agents ``AgentExecutorComponent`` should call. Uses the Qwen3
reasoning trace during planning, streamed into the AG-UI bridge as a
``REASONING_*`` event sequence when a bridge is provided.
"""

from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.stubs.qwen3 import StubQwen3Generator


@component
class MasterAgentComponent:
    def __init__(self, llm: StubQwen3Generator | None = None):
        self.llm = llm or StubQwen3Generator()

    @component.output_types(plan_json=dict)
    def run(self, query: str, bridge: AGUIEventBridge | None = None):
        if bridge:
            reasoning_tokens: list[str] = []
            plan = self.llm.plan(
                query,
                streaming_callback=lambda chunk: reasoning_tokens.append(chunk.content),
            )
            bridge.reasoning("".join(reasoning_tokens))
        else:
            plan = self.llm.plan(query)

        return {"plan_json": plan}
