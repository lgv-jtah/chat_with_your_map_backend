"""Tier 1 on Munich's ``buildings`` dataset (CHAT-12) end to end against a
fake GeoServer that serves the recorded sample of
``ascend_gebaeude_gfi_wfs`` (tests/fixtures/munich/README.md). The LLM
only translates the question; the expected numbers are literals computed
once from the 25 recorded features (shapely/plain counting)."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

from cwym_agent.city_profile import load_profile
from cwym_agent.geodata.analysis_text import render
from cwym_agent.geodata.filter_agent import FilterAgent
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.tier1 import Tier1Analyzer, load_datasets
from cwym_agent.geodata.types import LayerMatch, LocationMatch

FIXTURES = Path(__file__).parent / "fixtures" / "munich"
PROFILE = load_profile("munich")
DESCRIBE = (FIXTURES / "wfs_ascend_gebaeude_describe.xml").read_text(encoding="utf-8")
FEATURES = json.loads((FIXTURES / "wfs_ascend_gebaeude_sample.json").read_text(encoding="utf-8"))["features"]
BUILDINGS = LayerMatch("ASCEND Gebäudeinfo GFI", "gsm:ascend_gebaeude_gfi", 0.9)

_FES = "{http://www.opengis.net/fes/2.0}"
_COMPARISONS = {
    "PropertyIsEqualTo": lambda a, b: a == b,
    "PropertyIsNotEqualTo": lambda a, b: a != b,
    "PropertyIsLessThan": lambda a, b: a < b,
    "PropertyIsLessThanOrEqualTo": lambda a, b: a <= b,
    "PropertyIsGreaterThan": lambda a, b: a > b,
    "PropertyIsGreaterThanOrEqualTo": lambda a, b: a >= b,
}
_NUMERIC = ("int", "long", "double")


def _field_types() -> dict[str, str]:
    root = ET.fromstring(DESCRIBE)
    return {e.get("name"): e.get("type").split(":")[-1] for e in root.iter("{http://www.w3.org/2001/XMLSchema}element") if e.get("type")}


FIELD_TYPES = _field_types()


def _matches(node: ET.Element, properties: dict) -> bool:
    """A minimal FES 2.0 evaluation; ``Intersects`` is true because the
    server only gets the bounding box and the outline is checked locally."""

    tag = node.tag.removeprefix(_FES)
    if tag == "And":
        return all(_matches(child, properties) for child in node)
    if tag == "Intersects":
        return True
    name = node.find(f"{_FES}ValueReference").text
    literal = node.find(f"{_FES}Literal").text or ""
    value = properties.get(name)
    if value is None:
        return False
    if tag == "PropertyIsLike":
        return literal.strip("*").casefold() in str(value).casefold()
    if FIELD_TYPES[name] in _NUMERIC:
        return _COMPARISONS[tag](value, float(literal))
    if node.get("matchCase") == "false":
        value, literal = str(value).casefold(), literal.casefold()
    return _COMPARISONS[tag](value, literal)


class FakeGeoServer:
    def __init__(self, describe_status: int = 200):
        self.describe_status = describe_status
        self.bodies: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(self.describe_status, text=DESCRIBE)
        body = request.content.decode("utf-8")
        self.bodies.append(body)
        root = ET.fromstring(body)
        filter_node = root.find(f".//{_FES}Filter")
        matching = [
            f for f in FEATURES if filter_node is None or _matches(filter_node[0], f["properties"])
        ]
        if root.get("resultType") == "hits":
            return httpx.Response(200, text=f'<wfs:FeatureCollection numberMatched="{len(matching)}"/>')
        if root.get("outputFormat") == "application/geo+json":
            return httpx.Response(400, text="Failed to find response for output format application/geo+json")
        start, count = int(root.get("startIndex", 0)), int(root.get("count"))
        page = matching[start : start + count]
        return httpx.Response(200, json={"type": "FeatureCollection", "features": page, "numberMatched": len(matching)})

    @property
    def feature_requests(self) -> list[str]:
        return [b for b in self.bodies if 'resultType="hits"' not in b]


class _Llm:
    def __init__(self, reply):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.prompts = []

    def generate(self, prompt, max_tokens=None, label="generate", system_prompt=None, reasoning=True):
        self.prompts.append((label, prompt))
        return self.reply


def _setup(reply, settings: GeodataSettings | None = None, describe_status: int = 200):
    server = FakeGeoServer(describe_status)
    client = httpx.Client(transport=httpx.MockTransport(server.handler))
    agent = FilterAgent(PROFILE.prompt.area_terms, settings or GeodataSettings(), llm=_Llm(reply), http_client=client)
    return Tier1Analyzer(agent, load_datasets(PROFILE.datasets), PROFILE.name), server


def _area(file: str, kind: str, name: str | None = None) -> LocationMatch:
    features = json.loads((FIXTURES / file).read_text(encoding="utf-8"))["features"]
    feature = features[0] if name is None else next(f for f in features if f["properties"]["sb_name"] == name)
    return LocationMatch(name or "Harthof", 48.2, 11.56, 0.9, boundary=feature["geometry"], kind=kind)


HARTHOF = _area("wfs_projektgebiet.json", "projektgebiet")
MILBERTSHOFEN = _area("wfs_stadtbezirk.json", "stadtbezirk", "Milbertshofen-Am Hart")
# Every recorded building lies inside Harthof and Milbertshofen-Am Hart, so
# the local outline check needs a smaller area to drop something: a box
# around the five school buildings (Hugo-Wolf-Straße 70, Wegenerstraße 17).
_SCHOOLS = (11.56864586 - 0.0003, 48.20621368 - 0.0003, 11.56957881 + 0.0003, 48.20774281 + 0.0003)
SCHOOL_BLOCK = LocationMatch(
    "Schulblock", 48.207, 11.569, 0.9, kind="stadtteil",
    boundary={
        "type": "Polygon",
        "coordinates": [[[_SCHOOLS[0], _SCHOOLS[1]], [_SCHOOLS[2], _SCHOOLS[1]], [_SCHOOLS[2], _SCHOOLS[3]], [_SCHOOLS[0], _SCHOOLS[3]], [_SCHOOLS[0], _SCHOOLS[1]]]],
    },
)


def _ask(reply, operation, settings=None, location=None, describe_status=200):
    analyzer, server = _setup(reply, settings, describe_status)
    answer = analyzer.analyze("q", operation, BUILDINGS, location)
    return answer, server


def _lines(text: str) -> dict[str, str]:
    return dict(line[2:].split(": ") for line in text.splitlines()[1:] if line.startswith("- "))


class TestCount:
    def test_whole_dataset(self):
        answer, server = _ask({"conditions": []}, "count")

        assert answer.status == "ok" and answer.computed.total == 25
        assert render(answer, "de").startswith("25 Gebäude")
        assert 'xmlns:app="gsm_wfs"' in server.bodies[0] and 'typeNames="app:ascend_gebaeude_gfi_wfs"' in server.bodies[0]

    def test_paged_with_start_index(self):
        answer, server = _ask({"conditions": []}, "count", settings=GeodataSettings(analysis_page_size=5))

        assert answer.computed.total == 25
        pages = [b for b in server.feature_requests if 'count="5"' in b and "application/json" in b]
        assert [re.search(r'startIndex="(\d+)"', b) and re.search(r'startIndex="(\d+)"', b).group(1) for b in pages] == [None, "5", "10", "15", "20"]

    def test_umlaut_attribute_goes_to_the_server(self):
        reply = {"conditions": [{"field": "gebäudezustand", "operator": "eq", "value": "Abgerissen"}]}

        answer, server = _ask(reply, "count")

        assert "<fes:ValueReference>gebäudezustand</fes:ValueReference>" in server.feature_requests[-1]
        assert answer.computed.total == 1
        assert render(answer, "de") == "1 Gebäude mit Gebäudezustand = Abgerissen."
        assert render(answer, "en") == "1 buildings with building condition = Abgerissen."

    @pytest.mark.parametrize("area", [HARTHOF, MILBERTSHOFEN])
    def test_area_sends_the_bounding_box_and_checks_the_outline(self, area):
        answer, server = _ask({"conditions": []}, "count", location=area)

        body = server.feature_requests[-1]
        assert "<fes:Intersects><fes:ValueReference>shape</fes:ValueReference><gml:Polygon" in body
        assert answer.computed.total == 25
        assert render(answer, "de") == f"25 Gebäude in {area.name}."

    def test_the_outline_drops_buildings_outside(self):
        answer, _ = _ask({"conditions": []}, "count", location=SCHOOL_BLOCK)

        assert answer.computed.total == 5
        assert {f["properties"]["adresse"] for f in answer.features} == {"Wegenerstraße 17", "Hugo-Wolf-Straße 70"}


class TestOperations:
    def test_distribution_of_the_energy_class(self):
        answer, _ = _ask({"attribute": "energieeffizienzklasse"}, "distribution")

        expected = {"A": "1", "B": "1", "C": "3", "D": "1", "E": "1", "F": "1", "G": "1", "H": "5", "keine Angabe": "11"}
        assert _lines(render(answer, "de")) == expected
        assert list(_lines(render(answer, "de"))) == list(expected)  # order A..H, then "keine Angabe"
        assert render(answer, "de").startswith("Energieeffizienzklasse bei 25 Gebäude:")
        assert _lines(render(answer, "en"))["not specified"] == "11"
        assert answer.display_layer == "gsm:ascend_baugruppen_energieeffizienzklasse"

    def test_distribution_of_the_year_in_decades(self):
        answer, _ = _ask({"attribute": "baujahr"}, "distribution")

        assert _lines(render(answer, "de")) == {
            "1950–1959": "8", "1960–1969": "8", "1970–1979": "1", "1990–1999": "1",
            "2000–2009": "2", "2010–2019": "2", "2020–2029": "3",
        }
        assert answer.display_layer == "gsm:ascend_baugruppen_baujahr"

    def test_top_floors(self):
        answer, _ = _ask({"attribute": "geschosse", "n": 3, "order": "desc"}, "top")

        assert render(answer, "de") == (
            "Die 3 Gebäude mit dem höchsten Wert für Geschosse:\n"
            "1. Feuchtwangerstraße 10: 9\n"
            "2. Neuherbergstraße 109: 9\n"
            "3. Schleißheimer Straße 450: 8\n\n"
            "1 weitere Gebäude haben denselben Wert wie Platz 3."
        )

    @pytest.mark.parametrize(
        "operation, de, en",
        [
            ("min", "Kleinster Wert für PV-Leistung (kWp): 2,2 (1 Gebäude: Hugo-Wolf-Straße 16).", "Lowest PV output (kWp): 2.2 "),
            ("max", "Größter Wert für PV-Leistung (kWp): 64,4 (1 Gebäude: Feuchtwangerstraße 7).", "Highest PV output (kWp): 64.4 "),
            ("average", "Durchschnitt von PV-Leistung (kWp): 36,52 (aus 7 von 25 Gebäude).", "Average PV output (kWp): 36.52 (from 7 of 25 buildings)."),
            ("sum", "Summe von PV-Leistung (kWp): 255,64 (aus 7 von 25 Gebäude).", "Sum of PV output (kWp): 255.64 (from 7 of 25 buildings)."),
        ],
    )
    def test_pv_output_is_a_string_on_the_server_and_parsed_with_a_decimal_comma(self, operation, de, en):
        answer, _ = _ask({"attribute": "pv_leistung"}, operation)

        text = render(answer, "de")
        assert text.startswith(de)
        assert "18 Gebäude ohne Angabe zu PV-Leistung (kWp) sind nicht berücksichtigt." in text
        assert render(answer, "en").startswith(en)

    def test_an_ordinal_range_is_filtered_locally(self):
        reply = {"conditions": [{"field": "energieeffizienzklasse", "operator": "gt", "value": "E"}]}

        answer, server = _ask(reply, "count")

        assert answer.computed.total == 7  # F, G and five times H
        assert all("energieeffizienzklasse" not in body for body in server.bodies)
        assert render(answer, "de") == "7 Gebäude mit Energieeffizienzklasse > E."

    def test_lookup_returns_every_building_at_the_address(self):
        answer, _ = _ask({"lookup": "Hugo-Wolf-Straße 70"}, "lookup")

        text = render(answer, "de")
        assert text.startswith("Hugo-Wolf-Straße 70: 3 Gebäude gefunden.")
        assert text.count("\n- Hugo-Wolf-Straße 70:") == 3
        assert len(answer.features) == 3

    def test_lookup_of_an_unknown_house_number_lists_the_neighbours(self):
        answer, _ = _ask({"lookup": "Hugo-Wolf-Straße 99"}, "lookup")

        assert not answer.features
        assert render(answer, "de") == (
            "Zu „Hugo-Wolf-Straße 99“ habe ich keine Gebäude gefunden. "
            "Vorhanden sind: Hugo-Wolf-Straße 16, Hugo-Wolf-Straße 34, Hugo-Wolf-Straße 70."
        )

    def test_the_schema_service_failing_is_admitted(self):
        answer, _ = _ask({"conditions": []}, "count", describe_status=503)

        assert answer.status == "service_error"
        assert render(answer, "de").startswith("Die Daten konnten gerade nicht vollständig abgerufen werden")
        assert not re.search(r"\d", render(answer, "de"))
