"""Unit tests for ``MasterAgentComponent``'s context-aware prompt
building (cmym-agent-architecture.md Section 10.5)."""

from cwym_agent.city_profile import load_profile
from cwym_agent.components.master_agent import MasterAgentComponent
from cwym_agent.geodata.conversation_context import ConversationContext, MapStateIn
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.types import LayerMatch

_PROFILE = load_profile("hamburg")


class _RecordingLlm:
    def __init__(self):
        self.prompts: list[str] = []

    def plan(self, prompt: str, *, system_prompt, tools, streaming_callback=None):
        self.prompts.append(prompt)
        return {"steps": []}


class TestBuildPlanningPrompt:
    def test_no_context_leaves_query_untouched(self):
        llm = _RecordingLlm()
        component = MasterAgentComponent(_PROFILE, llm=llm)

        component.run("Wo finde ich Schulen?")

        assert llm.prompts == ["Wo finde ich Schulen?"]

    def test_empty_context_leaves_query_untouched(self):
        """First turn: no active layers, no filters, no history - the
        prompt must be exactly what it was before this feature existed,
        not an empty '## Aktueller Kartenzustand' header."""

        llm = _RecordingLlm()
        component = MasterAgentComponent(_PROFILE, llm=llm)
        context = ConversationContext(map_state=MapStateIn(), history=[])

        component.run("Wo finde ich Schulen?", context=context)

        assert llm.prompts == ["Wo finde ich Schulen?"]

    def test_active_layer_and_history_are_prepended(self):
        llm = _RecordingLlm()
        component = MasterAgentComponent(_PROFILE, llm=llm)
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
        component = MasterAgentComponent(_PROFILE, llm=llm)
        context = ConversationContext(map_state=MapStateIn(active_layer_ids=["1732"]), history=[])

        result = component.run("Kontaktformular öffnen", context=context)

        assert result["plan_json"] == {"steps": []}
        # The prompt sent to the LLM is enriched, but run() doesn't leak
        # it anywhere else - only plan_json is returned.
        assert "Kontaktformular öffnen" in llm.prompts[0]


class _FixedPlanLlm:
    def __init__(self, steps: list[dict]):
        self.steps = steps

    def plan(self, prompt: str, *, system_prompt, tools, streaming_callback=None):
        return {"steps": list(self.steps)}


def _planned(steps: list[dict], query: str, **kwargs) -> list[dict]:
    component = MasterAgentComponent(_PROFILE, llm=_FixedPlanLlm(steps))
    return component.run(query, **kwargs)["plan_json"]["steps"]


_LAYER = {"agent_name": "layer_resolver", "args": {"query": "Energieklassen"}}


class TestMissingAnalysisStep:
    def test_distribution_added(self):
        query = "Wie verteilen sich die Energieklassen?"
        assert _planned([_LAYER], query) == [
            _LAYER,
            {"agent_name": "filter_agent", "args": {"query": query, "operation": "distribution"}},
        ]

    def test_count_added(self):
        query = "Wie viele Schulen in Altona?"
        steps = _planned([{"agent_name": "layer_resolver", "args": {"query": "Schulen"}}], query)
        assert steps[-1] == {"agent_name": "filter_agent", "args": {"query": query, "operation": "count"}}

    def test_existing_filter_agent_not_duplicated(self):
        steps = [_LAYER, {"agent_name": "filter_agent", "args": {"query": "x", "operation": "count"}}]
        assert _planned(steps, "Wie viele Schulen in Altona?") == steps

    def test_plain_display_unchanged(self):
        assert _planned([_LAYER], "Zeige die Energieeffizienzklasse") == [_LAYER]

    def test_udp_analysis_unchanged(self):
        steps = [_LAYER, {"agent_name": "udp_analysis", "args": {"query": "x"}}]
        assert _planned(steps, "Wie viele Schulen in Altona?") == steps

    def test_follow_up_filter_agent_without_layer_resolver_unchanged(self):
        steps = [{"agent_name": "filter_agent", "args": {"layerId": "1732"}}]
        assert _planned(steps, "Wie viele davon?") == steps

    def test_empty_plan_stays_empty(self):
        assert _planned([], "Wie viele Schulen in Altona?") == []

    def test_context_wrapped_prompt_uses_raw_query(self):
        query = "Wie viele Schulen in Altona?"
        context = ConversationContext(
            map_state=MapStateIn(active_layer_ids=["1732"]), history=[("user", "hallo")]
        )
        steps = _planned([_LAYER], query, context=context)
        assert steps[-1]["args"]["query"] == query
