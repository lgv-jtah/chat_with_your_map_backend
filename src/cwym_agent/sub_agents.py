"""Shared sub-agent definitions.

Single source of truth for the sub-agents the pipeline can invoke
(``location_extractor``, ``layer_resolver``, ``action_detector``,
``filter_agent``, ``udp_analysis``): their names, human-readable descriptions and JSON
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
            "Navigieren, Filtern, Anzeigen/Öffnen einer Ebene). Auch "
            "aufrufen, wenn der Nutzer ein Werkzeug des Kartenportals "
            "öffnen möchte: Kontaktformular, Drucken, Legende, Teilen "
            "oder Impressum/Über uns (z.B. 'Öffne das Kontaktformular', "
            "'Öffne die Legende') - das gehört zur Karte und ist kein "
            "Small Talk."
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
                "clearFilterLayerIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "IDs von Ebenen mit aktivem Filter (aus 'Aktive Filter' im "
                        "Kontext), deren Filter laut Anfrage aufgehoben werden soll, "
                        "sodass wieder alle Objekte der Ebene angezeigt werden (z.B. "
                        "'zeig wieder alle', 'Filter entfernen', 'ohne Filter'). Nur "
                        "füllen, wenn die Anfrage das explizit so sagt."
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
    SubAgentSpec(
        name="udp_analysis",
        description=(
            "Beantwortet ANALYTISCHE Fragen zu Hamburgs Geodaten mit einer echten "
            "Datenanalyse der Urban Data Platform: Anzahlen ('Wie viele ...?'), "
            "Summen, Flächen, Längen, größte/kleinste, nächstgelegene, Dichte, "
            "Vergleiche zwischen Gebieten. Beispiel: 'Wie viele StadtRAD-Stationen "
            "gibt es in Altona?'. Zeigt das Ergebnis auch auf der Karte. Dauert 1-3 "
            "Minuten. NICHT verwenden zum bloßen Anzeigen, Filtern oder Navigieren - "
            "dafür gibt es die anderen Werkzeuge. Wenn du udp_analysis aufrufst, rufe "
            "KEIN anderes Werkzeug auf. Auch verwenden, wenn der Nutzer auf eine "
            "Rückfrage aus einer vorherigen Analyse antwortet."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Die Frage des Nutzers, wörtlich und vollständig - bei einer "
                        "Antwort auf eine Rückfrage die Antwort des Nutzers."
                    ),
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
