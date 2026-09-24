"""Unit tests for ``MasterAgentComponent``'s context-aware prompt
building (cmym-agent-architecture.md Section 10.5)."""

from cwym_agent.components.master_agent import MasterAgentComponent
from cwym_agent.geodata.conversation_context import ConversationContext, MapStateIn
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.types import LayerMatch


class _RecordingLlm:
    def __init__(self):
        self.prompts: list[str] = []

    def plan(self, prompt: str, streaming_callback=None):
        self.prompts.append(prompt)
        return {"steps": []}


class TestBuildPlanningPrompt:
    def test_no_context_leaves_query_untouched(self):
        llm = _RecordingLlm()
        component = MasterAgentComponent(llm=llm)

        component.run("Wo finde ich Schulen?")

        assert llm.prompts == ["Wo finde ich Schulen?"]

    def test_empty_context_leaves_query_untouched(self):
        """First turn: no active layers, no filters, no history - the
        prompt must be exactly what it was before this feature existed,
        not an empty '## Aktueller Kartenzustand' header."""

        llm = _RecordingLlm()
        component = MasterAgentComponent(llm=llm)
        context = ConversationContext(map_state=MapStateIn(), history=[])

        component.run("Wo finde ich Schulen?", context=context)

        assert llm.prompts == ["Wo finde ich Schulen?"]

    def test_active_layer_and_history_are_prepended(self):
        llm = _RecordingLlm()
        component = MasterAgentComponent(llm=llm)
        context = ConversationContext(
            map_state=MapStateIn(active_layer_ids=["1732"]),
            history=[("user", "wo finde ich alle Schulen"), ("assistant", "Ich zeige dir Schulen.")],
        )
        catalog = LayerCatalog({"1732": LayerMatch("Schulen Hamburg", "1732", 1.0)})

        component.run("Kannst du mir nur die Gymnasien zeigen?", context=context, layer_catalog=catalog)

        prompt = llm.prompts[0]
        assert "Schulen Hamburg (ID 1732)" in prompt
        assert "Nutzer: wo finde ich alle Schulen" in prompt
        assert "Regeln für Folgefragen" in prompt
        assert 'Nutzeranfrage: "Kannst du mir nur die Gymnasien zeigen?"' in prompt

    def test_query_used_downstream_stays_the_bare_query(self):
        """The augmented prompt only goes to the LLM - result_aggregator/
        command_builder/text_response still key off the original query
        text (e.g. for OPEN_TOOL keyword matching), never the enriched
        prompt."""

        llm = _RecordingLlm()
        component = MasterAgentComponent(llm=llm)
        context = ConversationContext(map_state=MapStateIn(active_layer_ids=["1732"]), history=[])

        result = component.run("Kontaktformular öffnen", context=context)

        assert result["plan_json"] == {"steps": []}
        # The prompt sent to the LLM is enriched, but run() doesn't leak
        # it anywhere else - only plan_json is returned.
        assert "Kontaktformular öffnen" in llm.prompts[0]
