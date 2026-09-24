"""``TextResponseComponent``: generates the final German-language reply.

Builds the response prompt from the aggregated sub-agent results and
generates the final German reply, streamed token by token into the AG-UI
bridge as a ``TEXT_MESSAGE_*`` event sequence when a bridge is provided.

Takes ``CommandBuilderComponent``'s actual ``commands`` output as an
input (not just ``aggregated``) so the reply is grounded in what really
happened on the map, not just in what sub-agents *resolved* - found
live: a follow-up query the planner couldn't act on (no prior-turn
context - see ``cwym-agent-architecture.md``'s open question on session
memory) still confidently replied "Klar, ich zeige dir jetzt nur die
Gymnasien", even though zero ``MapCommand``s were built and the map
never changed. ``aggregated`` facts (a resolved layer/location/action)
do not by themselves guarantee a command was emitted - e.g. an action
that's ``None`` or not in ``_ACTIONABLE_TYPES``, or a filter attempt
against a non-OAF layer, still leaves facts partially populated while
``commands`` stays empty. ``commands`` is the ground truth this
component narrates from.
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
    def run(
        self,
        aggregated: dict,
        query: str,
        commands: list | None = None,
        bridge: AGUIEventBridge | None = None,
    ):
        prompt = self._build_response_prompt(aggregated, query, commands or [])

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

    def _build_response_prompt(self, aggregated: dict, query: str, commands: list) -> str:
        """Build the NLG instruction prompt handed to ``self.llm.generate``.

        Describes what was resolved (location/layer/action, if any) so
        the LLM can phrase a natural German reply - this prompt is an
        *instruction*, not the reply itself; the real Qwen3 client
        generates the actual text from it.

        ``commands`` gates which prompt shape is used at all: resolved
        facts alone (a layer name, a location) are not proof that
        anything was actually pushed to the map - only a non-empty
        ``commands`` list is. Without this gate, the LLM would happily
        narrate success from partial facts even when zero MapCommands
        were built.
        """

        if not commands:
            return (
                f"Nutzeranfrage: \"{query}\"\n"
                "Es konnte KEINE Aktion auf der Karte ausgeführt werden - "
                "weder eine Kartenebene aktiviert noch gefiltert noch "
                "navigiert. Antworte kurz und ehrlich auf Deutsch: "
                "Behaupte NICHT, dass die Karte bereits angepasst wurde oder "
                "sich etwas geändert hat. Erkläre knapp, dass das gerade "
                "nicht möglich war, und frage bei Bedarf nach mehr Details "
                "(z. B. einem konkreten Ort oder einer Kartenebene)."
            )

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
            # number_matched (OGC API Features' numberMatched) is optional
            # per spec - fall back to the returned feature count when the
            # server didn't report it, rather than printing "None Treffer".
            count = filter_result.number_matched
            if count is None:
                count = len(filter_result.features.get("features", []))
            facts.append(f"Gefilterte Ergebnisse: {count} Treffer")

        resolved = "; ".join(facts)
        return (
            f"Nutzeranfrage: \"{query}\"\n"
            f"Aufgelöste Informationen: {resolved}\n"
            "Formuliere eine kurze, natürliche deutsche Antwort, die "
            "dem Nutzer mitteilt, was auf der Karte angezeigt wird."
        )
