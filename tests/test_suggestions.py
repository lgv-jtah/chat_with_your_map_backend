"""Follow-up suggestions at the end of a reply (CHAT-14)."""

import pytest
from haystack.dataclasses import StreamingChunk

from cwym_agent.city_profile import load_profile
from cwym_agent.components.suggestions import SuggestionSplitter, parse_items, split
from cwym_agent.components.text_response import TextResponseComponent
from cwym_agent.geodata.types import LayerMatch

_REPLY = "Ich zeige dir 48 StadtRAD-Stationen in Altona.\n\nVORSCHLÄGE:\n- Zeige Schulen in Altona\n- Wie viele Kitas gibt es dort?\n"


def _stream(text: str, size: int) -> tuple[str, list[str]]:
    splitter = SuggestionSplitter()
    shown = "".join(splitter.feed(text[i : i + size]) for i in range(0, len(text), size))
    rest, items = splitter.finish()
    return shown + rest, items


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11, 1000])
def test_the_marker_never_reaches_the_user_however_the_chunks_fall(size):
    shown, items = _stream(_REPLY, size)

    assert shown.rstrip() == "Ich zeige dir 48 StadtRAD-Stationen in Altona."
    assert items == ["Zeige Schulen in Altona", "Wie viele Kitas gibt es dort?"]


def test_holding_back_doesnt_delay_more_than_a_marker_length():
    splitter = SuggestionSplitter()

    shown = splitter.feed("Ich zeige dir die Schulen in Altona auf der Karte.")

    assert shown.startswith("Ich zeige dir die Schulen in Altona")


@pytest.mark.parametrize("marker", ["VORSCHLÄGE:", "Vorschläge:", "VORSCHLAEGE:", "SUGGESTIONS:", "Suggestions :"])
def test_marker_variants(marker):
    answer, items = split(f"Here are the hospitals.\n\n{marker}\n- Show schools nearby")

    assert answer == "Here are the hospitals." and items == ["Show schools nearby"]


def test_no_marker_means_everything_is_answer():
    assert _stream("Auf der Karte siehst du 5 Krankenhäuser.", 4) == ("Auf der Karte siehst du 5 Krankenhäuser.", [])


def test_items_are_cleaned_and_limited():
    block = "\n1. „Zeige Schulen“\n* Zeige Kitas\n- Zeige Kitas\n• " + "x" * 121 + "\n- Zeige Parks\n- Zeige Seen\n- Fünf"

    assert parse_items(block) == ["Zeige Schulen", "Zeige Kitas", "Zeige Parks"]


# --- through the reply component ----------------------------------------------------------

class _StreamingLlm:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def generate(self, prompt, streaming_callback=None, max_tokens=None, label="generate", system_prompt=None):
        self.prompts.append(prompt)
        if streaming_callback:
            for i in range(0, len(self.reply), 4):
                streaming_callback(StreamingChunk(content=self.reply[i : i + 4]))
        return self.reply


class _Bridge:
    def __init__(self):
        self.events, self.commands = [], []

    def emit(self, event):
        self.events.append(event)

    def command(self, cmd):
        self.commands.append(cmd)


_AGGREGATED = {"query": "q", "locations": [], "layer": LayerMatch("StadtRAD-Stationen", "27764", 0.9), "action": None, "filter_result": None}
_COMMANDS = [{"type": "ACTIVATE_LAYER", "payload": {"layerIds": ["27764"]}}]


def _reply(supported, bridge=None):
    llm = _StreamingLlm(_REPLY)
    result = TextResponseComponent(load_profile("hamburg"), llm=llm).run(
        _AGGREGATED, "q", commands=_COMMANDS, bridge=bridge, supported_commands=supported
    )
    return llm.prompts[0], result["response_text"]


def test_supporting_clients_get_suggestions_as_a_command():
    bridge = _Bridge()

    prompt, text = _reply(frozenset({"SHOW_SUGGESTIONS"}), bridge)

    assert "VORSCHLÄGE:" in prompt
    deltas = "".join(e.delta for e in bridge.events if type(e).__name__ == "TextMessageContentEvent")
    assert "VORSCHL" not in deltas and "Zeige Schulen" not in deltas
    assert text == deltas
    assert type(bridge.events[-1]).__name__ == "TextMessageEndEvent"
    [command] = bridge.commands
    assert command["type"] == "SHOW_SUGGESTIONS"
    assert command["payload"]["items"] == ["Zeige Schulen in Altona", "Wie viele Kitas gibt es dort?"]


def test_without_a_bridge_the_block_is_cut_too():
    _, text = _reply(frozenset({"SHOW_SUGGESTIONS"}))

    assert text == "Ich zeige dir 48 StadtRAD-Stationen in Altona."


def test_older_clients_are_not_asked_for_suggestions():
    bridge = _Bridge()

    prompt, _ = _reply(None, bridge)

    assert "VORSCHLÄGE" not in prompt and bridge.commands == []
