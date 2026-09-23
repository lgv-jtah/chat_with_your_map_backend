"""``TextResponseComponent``: generates the final German-language reply.

Builds the response prompt from the aggregated sub-agent results and
generates the final German reply, streamed token by token into the AG-UI
bridge as a ``TEXT_MESSAGE_*`` event sequence when a bridge is provided.
"""

import uuid
from typing import Any

from ag_ui.core import TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent
from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.llm.qwen3_client import Qwen3Client


@component
class TextResponseComponent:
    def __init__(self, llm: Any | None = None):
        self.llm = llm if llm is not None else Qwen3Client()

    @component.output_types(response_text=str)
    def run(self, aggregated: dict, query: str, bridge: AGUIEventBridge | None = None):
        prompt = self._build_response_prompt(aggregated, query)

        if bridge:
            message_id = str(uuid.uuid4())
            bridge.emit(TextMessageStartEvent(message_id=message_id))

            full_text: list[str] = []

            def on_token(chunk):
                full_text.append(chunk.content)
                bridge.emit(TextMessageContentEvent(message_id=message_id, delta=chunk.content))

            self.llm.generate(prompt, streaming_callback=on_token)
            bridge.emit(TextMessageEndEvent(message_id=message_id))
            response_text = "".join(full_text)
        else:
            response_text = self.llm.generate(prompt)

        return {"response_text": response_text}

    def _build_response_prompt(self, aggregated: dict, query: str) -> str:
        """Build the NLG instruction prompt handed to ``self.llm.generate``.

        Describes what was resolved (location/layer/action, if any) so
        the LLM can phrase a natural German reply - this prompt is an
        *instruction*, not the reply itself; the real Qwen3 client
        generates the actual text from it.
        """

        locations = aggregated.get("locations") or []
        layer = aggregated.get("layer")
        action = aggregated.get("action")
        filter_result = aggregated.get("filter_result")

        facts: list[str] = []
        if locations:
            facts.append(f"Ort: {locations[0].name}")
        if layer:
            facts.append(f"Kartenebene: {layer.name}")
        if action:
            facts.append(f"Aktion: {action.action_type}")
        if filter_result:
            facts.append(f"Gefilterte Ergebnisse: {filter_result.number_matched} Treffer")

        if facts:
            resolved = "; ".join(facts)
            return (
                f"Nutzeranfrage: \"{query}\"\n"
                f"Aufgelöste Informationen: {resolved}\n"
                "Formuliere eine kurze, natürliche deutsche Antwort, die "
                "dem Nutzer mitteilt, was auf der Karte angezeigt wird."
            )

        return (
            f"Nutzeranfrage: \"{query}\"\n"
            "Es konnten keine Ort/Kartenebene/Aktion aus der Anfrage "
            "aufgelöst werden. Antworte kurz und freundlich auf Deutsch "
            "und frage bei Bedarf nach mehr Details."
        )
