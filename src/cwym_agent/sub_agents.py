"""Shared sub-agent definitions.

Single source of truth for the four sub-agents the pipeline can invoke
(``location_extractor``, ``layer_resolver``, ``action_detector``,
``filter_agent``): their names, human-readable descriptions and JSON
argument schema.

This is used both by ``Qwen3Client.plan()`` (to build the ``tools=``
list offered to the model for native function calling) and by
``AgentExecutorComponent`` (to dispatch each planned step to the
matching ``StubDataServices`` method) - keeping both in one place means
the planner's tool definitions and the executor's dispatch table cannot
drift apart.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    description: str
    parameters: dict


SUB_AGENTS: list[SubAgentSpec] = [
    SubAgentSpec(
        name="location_extractor",
        description=(
            "Extrahiert einen Ort/Stadtteil aus der Nutzeranfrage und löst "
            "ihn zu Koordinaten auf (z.B. 'Wandsbek', 'Altona', 'Hamburg')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Die Nutzeranfrage oder der relevante Ortsanteil davon.",
                }
            },
            "required": ["query"],
        },
    ),
    SubAgentSpec(
        name="layer_resolver",
        description=(
            "Findet per semantischer Suche die passende Kartenebene aus "
            "Hamburgs Geodaten-Katalog für einen locker formulierten "
            "Begriff (z.B. 'Fahrradstationen' -> 'StadtRAD-Stationen "
            "Hamburg')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Die Nutzeranfrage oder der relevante Themenanteil davon.",
                }
            },
            "required": ["query"],
        },
    ),
    SubAgentSpec(
        name="action_detector",
        description=(
            "Bestimmt, welche Kartenaktion der Nutzer möchte (z.B. "
            "Navigieren, Filtern, Anzeigen/Öffnen einer Ebene)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Die Nutzeranfrage oder der relevante Aktionsanteil davon.",
                },
                "deactivateLayerIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "IDs aktiver Ebenen (aus 'Aktive Ebenen' im Kontext), die laut "
                        "Anfrage NICHT MEHR angezeigt werden sollen (z.B. 'aber nicht "
                        "mehr Fahrradstationen', 'ohne X', 'blende X aus'). Nur füllen, "
                        "wenn die Anfrage das explizit so sagt - eine normale neue "
                        "Anfrage deaktiviert nichts."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    SubAgentSpec(
        name="filter_agent",
        description=(
            "Filtert eine Kartenebene anhand konkreter Bedingungen. IMMER "
            "aufrufen, wenn die Anfrage 'filter', 'filtere' oder ein "
            "gleichbedeutendes Wort enthält, oder sonst eine konkrete "
            "Einschränkung nennt statt nur eine Ebene anzuzeigen (z.B. "
            "'Krankenhäuser mit mehr als 100 Betten', 'Schulen in "
            "Wandsbek'). Rufe in diesem Fall IMMER auch layer_resolver "
            "auf (welche Ebene) und, falls ein Ort genannt wird, "
            "location_extractor (für eine räumliche Einschränkung) - "
            "filter_agent nutzt deren Ergebnisse."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Die Nutzeranfrage oder der relevante Filteranteil davon.",
                }
            },
            "required": ["query"],
        },
    ),
]

SUB_AGENT_NAMES: frozenset[str] = frozenset(spec.name for spec in SUB_AGENTS)


def as_openai_tools() -> list[dict]:
    """Render the sub-agent specs as OpenAI-style ``tools=`` definitions."""

    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in SUB_AGENTS
    ]
