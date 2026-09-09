"""``TextResponseComponent``: generates the final German-language reply.

Builds the response prompt from the aggregated sub-agent results and
generates the final German reply, streamed token by token into the AG-UI
bridge as a ``TEXT_MESSAGE_*`` event sequence when a bridge is provided.
"""

import uuid

from ag_ui.core import TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent
from haystack import component

from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.stubs.qwen3 import StubQwen3Generator


@component
class TextResponseComponent:
    def __init__(self, llm: StubQwen3Generator | None = None):
        self.llm = llm or StubQwen3Generator()

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
        locations = aggregated.get("locations") or []
        layer = aggregated.get("layer")

        if layer and locations:
            location_name = locations[0].name
            return (
                f"Ich habe deine Anfrage \"{query}\" bearbeitet und die "
                f"Ebene \"{layer.name}\" identifiziert, die am besten zu "
                f"deiner Suche passt. Ich zeige dir jetzt die relevanten "
                f"Ergebnisse in {location_name} an."
            )

        return (
            f"Ich habe deine Anfrage \"{query}\" verarbeitet und werde dir "
            "helfen, die relevanten Informationen auf der Karte anzuzeigen."
        )
