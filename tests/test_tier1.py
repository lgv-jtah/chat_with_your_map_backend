"""Tier 1 end to end below the planner (CHAT-11, architecture 14.4):
question -> extraction (fake LLM) -> complete data -> computation ->
fixed reply. Includes the rule 4 check: the numbers in the reply are
exactly the computed ones."""

import json
import re

import httpx
import pytest
from fixtures.datasets import buildings_profile

from cwym_agent.geodata.analysis_text import format_number, render
from cwym_agent.geodata.filter_agent import FilterAgent
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.tier1 import Tier1Analyzer, load_datasets
from cwym_agent.geodata.types import LayerMatch, LocationMatch

FIRST_ROW = {"type": "Polygon", "coordinates": [[[9.9995, 53.5995], [10.0075, 53.5995], [10.0075, 53.6015], [9.9995, 53.6015], [9.9995, 53.5995]]]}
NORDVIERTEL = LocationMatch("Nordviertel", 53.6, 10.0, 0.9, boundary=FIRST_ROW, kind="stadtteil")
BUILDINGS_LAYER = LayerMatch("Gebäude", "test_gebaeude", 0.9)


class _Llm:
    def __init__(self, reply):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.prompts = []

    def generate(self, prompt, max_tokens=None, label="generate", system_prompt=None):
        self.prompts.append((label, prompt))
        return self.reply


@pytest.fixture
def profile(tmp_path):
    return buildings_profile(tmp_path)


def _analyzer(profile, reply, settings=None, http_client=None):
    agent = FilterAgent(profile.prompt.area_terms, settings or GeodataSettings(), llm=_Llm(reply), http_client=http_client)
    return Tier1Analyzer(agent, load_datasets(profile.datasets), profile.name)


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:,\d+)?", text)


class TestDeclaredDataset:
    def test_count_with_condition_and_area(self, profile):
        reply = {"conditions": [{"field": "eigentuemer", "operator": "eq", "value": "Wohnbau"}], "attribute": None}
        analyzer = _analyzer(profile, reply)

        answer = analyzer.analyze("Wie viele Gebäude gehören Wohnbau im Nordviertel?", "count", BUILDINGS_LAYER, NORDVIERTEL)

        assert answer.status == "ok" and answer.computed.total == 3
        assert [f["properties"]["gebid"] for f in answer.features] == ["B01", "B02", "B03"]
        assert render(answer, "de") == "3 Gebäude in Nordviertel mit Eigentümer = Wohnbau."
        assert render(answer, "en") == "3 buildings in Nordviertel with owner = Wohnbau."
        label, prompt = analyzer.filter_agent.llm.prompts[0]
        assert label == "analysis_extraction"
        assert "- energieeffizienzklasse (Rangfolge) - Energieeffizienzklasse - Reihenfolge: A+, A, B" in prompt
        assert "- eigentuemer (Kategorie) - Eigentümer - Werte: Genossenschaft, Kirche, Stadt, Wohnbau" in prompt

    def test_distribution_reply_numbers_are_the_computed_ones(self, profile):
        answer = _analyzer(profile, {"attribute": "energieeffizienzklasse"}).analyze(
            "Wie verteilen sich die Energieklassen?", "distribution", BUILDINGS_LAYER
        )

        text = render(answer, "de")
        lines = dict(line[2:].split(": ") for line in text.splitlines()[1:] if line.startswith("- "))
        assert lines == {"A+": "1", "A": "1", "B": "1", "C": "1", "E": "1", "F": "1", "G": "1", "H": "3", "keine Angabe": "2"}
        assert {b.label or "keine Angabe": str(b.count) for b in answer.computed.buckets} == lines
        assert text.startswith("Energieeffizienzklasse bei 12 Gebäude:")
        assert answer.display_layer == "test_energieklasse"

    def test_top_reply(self, profile):
        answer = _analyzer(profile, {"attribute": "baujahr", "n": 2, "order": "asc"}).analyze(
            "Die zwei ältesten Gebäude", "top", BUILDINGS_LAYER
        )

        assert render(answer, "de") == (
            "Die 2 Gebäude mit dem niedrigsten Wert für Baujahr:\n"
            "1. Birkenstraße 2: 1940\n"
            "2. Lindenweg 1: 1952\n\n"
            "1 weitere Gebäude haben denselben Wert wie Platz 2."
        )
        assert [f["properties"]["gebid"] for f in answer.features] == ["B06", "B01"]

    def test_average_reply_states_what_was_left_out(self, profile):
        answer = _analyzer(profile, {"attribute": "pv_leistung"}).analyze("Durchschnittliche PV-Leistung", "average", None)

        text = render(answer, "de")
        assert text == (
            "Durchschnitt von PV-Leistung (kWp): 57,25 (aus 4 von 12 Gebäude). "
            "8 Gebäude ohne Angabe zu PV-Leistung (kWp) sind nicht berücksichtigt."
        )
        assert format_number(answer.computed.value, "de") in _numbers(text)
        assert render(answer, "en").startswith("Average PV output (kWp): 57.25 (from 4 of 12 buildings).")

    def test_ordinal_max_lists_every_feature(self, profile):
        answer = _analyzer(profile, {"attribute": "energieeffizienzklasse"}).analyze("Schlechteste Klasse?", "max", None)

        assert render(answer, "de").startswith("Größter Wert für Energieeffizienzklasse: H (3 Gebäude: Lindenweg 1, Lindenweg 3, Birkenstraße 2).")

    def test_lookup(self, profile):
        answer = _analyzer(profile, {"lookup": "Birkenstraße 4"}).analyze("Was ist mit Birkenstraße 4?", "lookup", None)

        text = render(answer, "de")
        assert text.startswith("Birkenstraße 4: 2 Gebäude gefunden.\n- Birkenstraße 4: Baujahr: 1955;")
        assert "Energieeffizienzklasse: A+" in text

    def test_lookup_unknown_number(self, profile):
        answer = _analyzer(profile, {"lookup": "Lindenweg 7"}).analyze("Lindenweg 7", "lookup", None)

        assert render(answer, "de") == (
            "Zu „Lindenweg 7“ habe ich keine Gebäude gefunden. "
            "Vorhanden sind: Lindenweg 1, Lindenweg 3, Lindenweg 5, Lindenweg 8/10, Lindenweg 17."
        )

    @pytest.mark.parametrize(
        "operation, reply, status",
        [
            ("distribution", {"attribute": None}, "no_attribute"),
            ("distribution", "kein JSON", "no_attribute"),
            ("average", {"attribute": "energieeffizienzklasse"}, "not_numeric"),
            ("top", {"attribute": "eigentuemer"}, "not_sortable"),
            ("distribution", {"attribute": "erfunden"}, "no_attribute"),
        ],
    )
    def test_honest_refusals(self, profile, operation, reply, status):
        answer = _analyzer(profile, reply).analyze("q", operation, BUILDINGS_LAYER)

        assert answer.status == status and answer.computed is None
        assert not _numbers(render(answer, "de")) or status == "no_attribute"

    def test_a_file_dataset_is_always_complete(self, profile):
        """It's in memory: the fetch cap only bounds services."""

        answer = _analyzer(profile, {"attribute": None}, GeodataSettings(analysis_max_features=5)).analyze(
            "Wie viele Gebäude?", "count", BUILDINGS_LAYER
        )

        assert answer.status == "ok" and answer.computed.total == 12

    def test_place_without_outline(self, profile):
        street = LocationMatch("Lindenweg", 53.6, 10.0, 0.9)

        answer = _analyzer(profile, {}).analyze("Wie viele Gebäude im Lindenweg?", "count", BUILDINGS_LAYER, street)

        assert answer.status == "no_area"
        assert render(answer, "de") == "Für „Lindenweg“ habe ich keine Gebietsgrenze, deshalb kann ich dort nichts auswerten."

    def test_the_city_itself_is_the_whole_dataset(self, profile):
        city = LocationMatch("Hamburg", 53.55, 9.99, 0.9, extent=profile.extent)

        answer = _analyzer(profile, {}).analyze("Wie viele Gebäude in Hamburg?", "count", BUILDINGS_LAYER, city)

        assert answer.computed.total == 12 and answer.area_name is None


class TestPlainLayer:
    _SCHOOLS = LayerMatch("Schulen", "1732", 0.9, typ="OAF", url="https://api.example", collection="schulen")

    def _client(self, number_matched=37, features=None):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/queryables"):
                return httpx.Response(200, json={"properties": {"art": {"type": "string"}, "geom": {"x-ogc-role": "primary-geometry"}}})
            limit = int(request.url.params.get("limit", 10))
            page = (features or [])[:limit]
            return httpx.Response(200, json={"type": "FeatureCollection", "numberMatched": number_matched, "features": page})

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_count_any_filterable_layer(self, profile):
        features = [{"type": "Feature", "geometry": None, "properties": {}} for _ in range(37)]
        analyzer = _analyzer(profile, "[]", http_client=self._client(37, features))

        answer = analyzer.analyze("Wie viele Schulen gibt es?", "count", self._SCHOOLS)

        assert answer.computed.total == 37 and len(answer.features) == 37
        assert render(answer, "de") == "„Schulen“: 37 Treffer."
        assert analyzer.filter_agent.llm.prompts[0][0] == "filter_condition_extraction"  # the existing prompt

    def test_too_many_to_fetch_uses_the_exact_server_total(self, profile):
        analyzer = _analyzer(
            profile, "[]", GeodataSettings(analysis_max_features=10, analysis_page_size=5), self._client(900, [])
        )

        answer = analyzer.analyze("Wie viele Schulen?", "count", self._SCHOOLS)

        assert answer.computed.total == 900 and answer.features == []

    def test_too_many_in_an_area_says_so(self, profile):
        analyzer = _analyzer(
            profile, "[]", GeodataSettings(analysis_max_features=10, analysis_page_size=5), self._client(900, [])
        )

        answer = analyzer.analyze("Wie viele Schulen im Nordviertel?", "count", self._SCHOOLS, NORDVIERTEL)

        assert answer.status == "too_many"
        text = render(answer, "de")
        assert text.startswith("Das sind zu viele Objekte in Nordviertel (mehr als 10)") and "keine Zahl" in text

    def test_other_operations_need_a_dataset(self, profile):
        answer = _analyzer(profile, "[]").analyze("Verteilung der Schularten", "distribution", self._SCHOOLS)

        assert answer.status == "needs_dataset"
        assert render(answer, "de").startswith("Für „Schulen“ kann ich nur zählen.")

    def test_not_filterable(self, profile):
        wms = LayerMatch("Luftbild", "1", 0.9, typ="WMS")

        assert _analyzer(profile, "[]").analyze("Wie viele?", "count", wms).status == "not_filterable"
