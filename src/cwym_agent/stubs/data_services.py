"""Stub data services.

Fakes the vector search over Hamburg's ~7,000 geodata layers and the
geodata/urban-data-platform API for location resolution, so the pipeline
shape can be built and tested before those real services are wired in.

The demo data intentionally mirrors the mock responses that already exist
in the ``chatWithYourMap`` Masterportal addon frontend
(``ChatWithYourMap.vue``), so that swapping the frontend from its own mock
mode to this backend keeps the same demo experience.
"""

from dataclasses import dataclass, field


@dataclass
class LocationMatch:
    name: str
    latitude: float
    longitude: float
    confidence: float


@dataclass
class LayerMatch:
    name: str
    layer_id: str
    confidence: float


@dataclass
class ActionMatch:
    action_type: str
    confidence: float


# Very small, deterministic "knowledge base" standing in for the real
# vector search over Hamburg's geodata layers and the geodata API.
_LOCATIONS: dict[str, LocationMatch] = {
    "wandsbek": LocationMatch("Wandsbek", 53.5760029, 10.0755348, 0.95),
    "altona": LocationMatch("Altona", 53.5503, 9.9352, 0.95),
    "hamburg": LocationMatch("Hamburg", 53.5511, 9.9937, 0.9),
}

_LAYERS: list[tuple[tuple[str, ...], LayerMatch]] = [
    (
        ("fahrrad", "bike", "fahrräder", "stadtrad"),
        LayerMatch("StadtRAD-Stationen Hamburg", "27764", 0.92),
    ),
    (
        ("krankenhaus", "krankenhäuser", "hospital"),
        LayerMatch("Krankenhäuser Hamburg", "18452", 0.9),
    ),
    (
        ("schule", "schulen", "school"),
        LayerMatch("Schulstandorte Hamburg", "20391", 0.88),
    ),
]

_ACTION_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("navigiere", "navigate", "fahre nach"), "Navigieren"),
    (("filter", "filtere"), "Filter"),
    (("öffne", "oeffne", "open"), "Öffne"),
]

_DEFAULT_LAYER = LayerMatch("Default Layer", "1234", 0.8)
_DEFAULT_LOCATION = LocationMatch("Hamburg", 53.5511, 9.9937, 0.9)
_DEFAULT_ACTION = "Zeigen"


class StubDataServices:
    """Fake replacement for the vector-search + geodata API layer.

    Every method mirrors the shape the real services are expected to
    return, so the pipeline components built against this stub don't need
    to change once real services are wired in - only this class gets
    replaced.
    """

    def resolve_locations(self, query: str) -> list[LocationMatch]:
        lowered = query.lower()

        for keyword, location in _LOCATIONS.items():
            if keyword in lowered:
                return [location]

        return [_DEFAULT_LOCATION]

    def resolve_layer(self, query: str) -> LayerMatch:
        lowered = query.lower()

        for keywords, layer in _LAYERS:
            if any(keyword in lowered for keyword in keywords):
                return layer

        return _DEFAULT_LAYER

    def detect_action(self, query: str) -> ActionMatch:
        lowered = query.lower()

        for keywords, action_type in _ACTION_KEYWORDS:
            if any(keyword in lowered for keyword in keywords):
                return ActionMatch(action_type, 0.9)

        return ActionMatch(_DEFAULT_ACTION, 0.85)
