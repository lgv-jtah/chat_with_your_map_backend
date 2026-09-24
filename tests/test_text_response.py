"""Unit tests for ``TextResponseComponent._build_response_prompt``.

Uses ``StubQwen3Generator`` (its ``generate`` just echoes the prompt back)
so assertions can inspect the actual prompt text the component built,
the same pattern used elsewhere for a cheap, deterministic LLM double.
"""

from cwym_agent.components.text_response import TextResponseComponent
from cwym_agent.geodata.types import ActionMatch, FilterResult, LayerMatch, LocationMatch
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
