"""Unit tests for ``TextResponseComponent._build_response_prompt``.

Uses ``StubQwen3Generator`` (its ``generate`` just echoes the prompt back)
so assertions can inspect the actual prompt text the component built,
the same pattern used elsewhere for a cheap, deterministic LLM double.
"""

from cwym_agent.components.text_response import TextResponseComponent
from cwym_agent.geodata.conversation_context import ConversationContext, MapStateIn
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.types import ActionMatch, AnalysisResult, FilterResult, LayerMatch, LocationMatch
from cwym_agent.stubs.qwen3 import StubQwen3Generator

_LOCATION = LocationMatch(name="Wandsbek", latitude=53.6, longitude=10.1, confidence=0.9)
_LAYER = LayerMatch(name="StadtRAD-Stationen Hamburg", layer_id="27764", confidence=0.9)
_ACTION = ActionMatch("Zeigen", 0.9)

component = TextResponseComponent(llm=StubQwen3Generator())


def _aggregated(**overrides) -> dict:
    base = {"query": "", "locations": [], "layer": None, "action": None, "filter_result": None}
    base.update(overrides)
    return base


class TestNoCommandsMeansNoClaimOfSuccess:
    def test_empty_commands_never_claims_the_map_changed(self):
        """The bug this fixes, live: a resolved layer fact existed in
        `aggregated`, but zero commands were actually built (e.g. no
        actionable `action_type`) - the old prompt still narrated
        success from the layer fact alone."""

        aggregated = _aggregated(query="Kannst du mir nur die Gymnasien zeigen?", layer=_LAYER)

        result = component.run(aggregated, aggregated["query"], commands=[])

        prompt = result["response_text"]
        assert "Behaupte NICHT" in prompt
        assert "Aufgelöste Informationen" not in prompt

    def test_none_commands_treated_same_as_empty_list(self):
        aggregated = _aggregated(query="Foo", action=_ACTION)

        result = component.run(aggregated, aggregated["query"], commands=None)

        assert "Behaupte NICHT" in result["response_text"]

    def test_facts_present_but_no_commands_still_refuses_to_claim_success(self):
        aggregated = _aggregated(
            query="Zeige mir Krankenhäuser", layer=_LAYER, locations=[_LOCATION], action=_ACTION
        )

        result = component.run(aggregated, aggregated["query"], commands=[])

        assert "Behaupte NICHT" in result["response_text"]


class TestContextAwareNarration:
    def test_empty_context_adds_nothing_to_prompt(self):
        aggregated = _aggregated(query="Zeige mir StadtRAD", layer=_LAYER, action=_ACTION)
        commands = [{"type": "ACTIVATE_LAYER", "payload": {"layerIds": ["27764"]}}]
        context = ConversationContext(map_state=MapStateIn(), history=[])

        result = component.run(aggregated, aggregated["query"], commands=commands, context=context)

        assert "Aktueller Kartenzustand" not in result["response_text"]

    def test_active_layer_context_prepended_to_prompt(self):
        aggregated = _aggregated(query="Nur die Gymnasien", action=_ACTION)
        commands = [{"type": "APPLY_FILTER", "payload": {}}]
        context = ConversationContext(map_state=MapStateIn(active_layer_ids=["1732"]), history=[])
        catalog = LayerCatalog({"1732": LayerMatch("Schulen Hamburg", "1732", 1.0)})

        result = component.run(
            aggregated, aggregated["query"], commands=commands, context=context, layer_catalog=catalog
        )

        prompt = result["response_text"]
        assert "Schulen Hamburg (ID 1732)" in prompt
        assert "beziehe dich bei einer Folgefrage" in prompt.lower() or "von den angezeigten" in prompt.lower()

    def test_context_note_also_applies_to_the_honesty_gate_prompt(self):
        """Even when nothing could be resolved, the model should still
        see the active-layer context if any exists - doesn't change the
        no-success rule, just gives it more to work with."""

        aggregated = _aggregated(query="Nur die Gymnasien")
        context = ConversationContext(map_state=MapStateIn(active_layer_ids=["1732"]), history=[])
        catalog = LayerCatalog({"1732": LayerMatch("Schulen Hamburg", "1732", 1.0)})

        result = component.run(
            aggregated, aggregated["query"], commands=[], context=context, layer_catalog=catalog
        )

        prompt = result["response_text"]
        assert "Schulen Hamburg (ID 1732)" in prompt
        assert "Behaupte NICHT" in prompt


class TestCommandsPresentNarratesFromFacts:
    def test_non_empty_commands_uses_resolved_facts_prompt(self):
        aggregated = _aggregated(query="Zeige mir StadtRAD", layer=_LAYER, action=_ACTION)
        commands = [{"type": "ACTIVATE_LAYER", "payload": {"layerIds": ["27764"]}}]

        result = component.run(aggregated, aggregated["query"], commands=commands)

        prompt = result["response_text"]
        assert "Aufgelöste Informationen" in prompt
        assert "StadtRAD-Stationen Hamburg" in prompt
        assert "Behaupte NICHT" not in prompt

    def test_filter_result_fact_uses_number_matched(self):
        filter_result = FilterResult(
            layer_id="1", features={"type": "FeatureCollection", "features": [{}]}, number_matched=42
        )
        aggregated = _aggregated(query="Filtere", filter_result=filter_result)
        commands = [{"type": "APPLY_FILTER", "payload": {}}]

        result = component.run(aggregated, aggregated["query"], commands=commands)

        assert "42 Treffer" in result["response_text"]


class TestClearFilterNarration:
    def test_cleared_filter_named_and_filter_action_not_mentioned(self):
        """"Filter entfernen" keyword-detects as action "Filter" - the
        prompt must say the filter was lifted, not name a Filter action."""

        aggregated = _aggregated(query="Filter entfernen", action=ActionMatch("Filter", 0.9, clear_filter_layer_ids=["1732"]))
        commands = [
            {"type": "CLEAR_FILTER", "payload": {"layerId": "1732"}},
            {"type": "ACTIVATE_LAYER", "payload": {"layerIds": ["1732"]}},
        ]
        catalog = LayerCatalog({"1732": LayerMatch("Schulen Hamburg", "1732", 1.0)})

        prompt = component.run(aggregated, aggregated["query"], commands=commands, layer_catalog=catalog)["response_text"]

        assert "Filter aufgehoben, wieder alle Objekte sichtbar: Schulen Hamburg" in prompt
        assert "Aktion: Filter" not in prompt


class TestAnalysisReply:
    class _NoLLM:
        def generate(self, *args, **kwargs):
            raise AssertionError("an analysis reply must not go through the LLM")

    def test_udp_answer_passed_through_verbatim(self):
        component = TextResponseComponent(llm=self._NoLLM())
        answer = "Im Bezirk Altona gibt es **19** StadtRAD-Stationen."
        aggregated = _aggregated(query="Wie viele?", analysis_result=AnalysisResult(answer=answer))

        assert component.run(aggregated, "Wie viele?", commands=[])["response_text"] == answer

    def test_failed_analysis_says_so(self):
        component = TextResponseComponent(llm=self._NoLLM())
        aggregated = _aggregated(query="Wie viele?", analysis_result=AnalysisResult(answer=None, error="timeout"))

        text = component.run(aggregated, "Wie viele?", commands=[])["response_text"]

        assert "keine Antwort" in text
        assert "nicht verändert" in text
