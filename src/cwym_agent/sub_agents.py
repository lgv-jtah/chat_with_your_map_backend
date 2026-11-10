"""Shared sub-agent definitions.

Single source of truth for the sub-agents the pipeline can invoke
(``location_extractor``, ``layer_resolver``, ``action_detector``,
``filter_agent``, ``udp_analysis``): their names, human-readable descriptions and JSON
argument schema.

``MasterAgentComponent`` renders them with the city profile's wording
(example places and layers) into the ``tools=`` list offered to the
model for native function calling; ``SUB_AGENT_NAMES`` is what
``Qwen3Client`` accepts back from the model.
"""

from dataclasses import dataclass

from cwym_agent.city_profile import CityProfile


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    description: str
    parameters: dict


def _quoted(names: list[str]) -> str:
    return ", ".join(f"'{name}'" for name in names)


def sub_agent_specs(profile: CityProfile) -> list[SubAgentSpec]:
    """The sub-agents, described with ``profile``'s wording."""

    wording = profile.prompt
    city_genitive = wording.city_genitive
    places = wording.example_places
    layer = wording.layer_example
    specs = [
        SubAgentSpec(
            name="location_extractor",
            description=(
                f"Extrahiert einen Ort/{wording.area_terms[0]} aus der Nutzeranfrage und löst "
                f"ihn zu Koordinaten auf (z.B. {_quoted([*places, profile.name])})."
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
                f"{city_genitive} Geodaten-Katalog für einen locker formulierten "
                f"Begriff (z.B. '{layer.term}' -> '{layer.layer_name}')."
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
                f"{places[0]}'). Rufe in diesem Fall IMMER auch layer_resolver "
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
    if profile.analysis_agent is not None:
        analysis_example = profile.analysis_agent.example_question
        analysis = SubAgentSpec(
            name="udp_analysis",
            description=(
                f"Beantwortet ANALYTISCHE Fragen zu {city_genitive} Geodaten mit einer echten "
                "Datenanalyse der Urban Data Platform: Anzahlen ('Wie viele ...?'), "
                "Summen, Flächen, Längen, größte/kleinste, nächstgelegene, Dichte, "
                f"Vergleiche zwischen Gebieten. Beispiel: '{analysis_example}'. "
                "Zeigt das Ergebnis auch auf der Karte. Dauert 1-3 "
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
        )
        specs.append(analysis)
    return specs


SUB_AGENT_NAMES: frozenset[str] = frozenset(
    {"location_extractor", "layer_resolver", "action_detector", "filter_agent", "udp_analysis"}
)


def as_openai_tools(specs: list[SubAgentSpec]) -> list[dict]:
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
        for spec in specs
    ]
