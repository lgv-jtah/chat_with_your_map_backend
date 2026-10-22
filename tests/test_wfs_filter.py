"""Unit tests for ``wfs_filter.py`` and FilterAgent's WFS path - pure XML
building/parsing plus orchestration against a mocked HTTP transport and a
fake LLM. The real behaviour of Hamburg's deegree WFS (namespace, axis
order, hits) was verified live while building this; see the module
docstring."""

import json

import httpx

from cwym_agent.geodata.filter_agent import FilterAgent
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import FilterCondition, LayerMatch, LocationMatch
from cwym_agent.geodata.wfs_filter import (
    WfsClient,
    boundary_to_gml,
    build_filter,
    build_get_feature,
    collect_examples,
    condition_to_fes,
    parse_feature_type_schema,
)

_NS = "https://registry.gdi-de.org/id/de.hh.up"

# Shape of deegree's DescribeFeatureType: all feature types of the service
# in one schema, each with an inline complexType.
_XSD = f"""<?xml version="1.0"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema" xmlns:gml="http://www.opengis.net/gml/3.2"
        targetNamespace="{_NS}">
  <element name="hh_schulen_eingaenge" substitutionGroup="gml:AbstractFeature">
    <complexType><complexContent><extension base="gml:AbstractFeatureType"><sequence>
      <element name="Schul_ID" minOccurs="0" type="string"/>
      <element name="geom" minOccurs="0" type="gml:PointPropertyType"/>
    </sequence></extension></complexContent></complexType>
  </element>
  <element name="staatliche_schulen" substitutionGroup="gml:AbstractFeature">
    <complexType><complexContent><extension base="gml:AbstractFeatureType"><sequence>
      <element name="the_geom" minOccurs="0" type="gml:PointPropertyType"/>
      <element name="schulname" minOccurs="0" type="string"/>
      <element name="kapitelbezeichnung" minOccurs="0" type="string"/>
      <element name="anzahl_schueler" minOccurs="0" type="integer"/>
    </sequence></extension></complexContent></complexType>
  </element>
</schema>"""

_SCHOOL_LAYER = LayerMatch(
    name="Grundschulen",
    layer_id="8712.1",
    confidence=1.0,
    typ="WFS",
    url="https://geodienste.hamburg.de/HH_WFS_Schulen",
    feature_type="staatliche_schulen",
    # deliberately the outdated namespace from services.json - the schema's
    # own targetNamespace must be used instead
    feature_ns="http://www.deegree.org/de.hh.up",
)

_HOHELUFT_WEST = LocationMatch(
    name="Hoheluft-West",
    latitude=53.58,
    longitude=9.97,
    confidence=0.9,
    boundary={
        "type": "Polygon",
        "coordinates": [[[9.955, 53.575], [9.975, 53.575], [9.975, 53.59], [9.955, 53.59], [9.955, 53.575]]],
    },
    kind="stadtteil",
)


def _feature(**properties) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [9.96, 53.58]}, "properties": properties}


class TestParseFeatureTypeSchema:
    def test_reads_only_the_requested_feature_type(self):
        schema = parse_feature_type_schema(_XSD, "staatliche_schulen")

        assert schema.namespace == _NS
        assert schema.geometry_field == "the_geom"
        assert schema.fields == {"schulname": "string", "kapitelbezeichnung": "string", "anzahl_schueler": "integer"}

    def test_unknown_type_or_bad_xml(self):
        assert parse_feature_type_schema(_XSD, "gibt_es_nicht") is None
        assert parse_feature_type_schema("<kein xml", "staatliche_schulen") is None


class TestCollectExamples:
    def test_keeps_only_category_like_string_fields(self):
        features = [
            _feature(kapitelbezeichnung="Grundschulen", schulname=f"Schule {i}", anzahl_schueler=i)
            for i in range(20)
        ] + [_feature(kapitelbezeichnung="Gymnasien", schulname="Schule X", anzahl_schueler=1)]
        fields = {"kapitelbezeichnung": "string", "schulname": "string", "anzahl_schueler": "integer"}

        assert collect_examples(features, fields) == {"kapitelbezeichnung": ["Grundschulen", "Gymnasien"]}


class TestConditionToFes:
    def test_eq_string_is_case_insensitive_and_escaped(self):
        assert condition_to_fes(FilterCondition("schulname", "eq", "A & <B>")) == (
            '<fes:PropertyIsEqualTo matchCase="false"><fes:ValueReference>schulname</fes:ValueReference>'
            "<fes:Literal>A &amp; &lt;B&gt;</fes:Literal></fes:PropertyIsEqualTo>"
        )

    def test_numeric_and_boolean(self):
        assert condition_to_fes(FilterCondition("anzahl_schueler", "gt", 500)) == (
            "<fes:PropertyIsGreaterThan><fes:ValueReference>anzahl_schueler</fes:ValueReference>"
            "<fes:Literal>500</fes:Literal></fes:PropertyIsGreaterThan>"
        )
        assert "<fes:Literal>true</fes:Literal>" in condition_to_fes(FilterCondition("kantine", "eq", True))

    def test_contains_escapes_wildcards(self):
        clause = condition_to_fes(FilterCondition("schulname", "contains", "50*"))

        assert 'wildCard="*"' in clause
        assert "<fes:Literal>*50\\**</fes:Literal>" in clause

    def test_unsupported_operator_or_value(self):
        assert condition_to_fes(FilterCondition("schulname", "between", "x")) is None
        assert condition_to_fes(FilterCondition("schulname", "eq", ["a"])) is None


class TestBoundaryToGml:
    def test_polygon_with_hole_in_lon_lat_order(self):
        gml = boundary_to_gml(
            {"type": "Polygon", "coordinates": [[[9.0, 53.0], [9.1, 53.0], [9.0, 53.1]], [[9.01, 53.01], [9.02, 53.01], [9.01, 53.02]]]}
        )

        assert gml.startswith('<gml:Polygon gml:id="cwym_area" srsName="EPSG:4326">')
        assert "<gml:posList>9.0 53.0 9.1 53.0 9.0 53.1</gml:posList>" in gml
        assert "<gml:interior>" in gml

    def test_multipolygon(self):
        gml = boundary_to_gml(
            {"type": "MultiPolygon", "coordinates": [[[[9.0, 53.0], [9.1, 53.0], [9.0, 53.1]]], [[[8.0, 52.0], [8.1, 52.0], [8.0, 52.1]]]]}
        )

        assert gml.startswith("<gml:MultiSurface")
        assert gml.count("<gml:surfaceMember>") == 2

    def test_other_geometry(self):
        assert boundary_to_gml({"type": "Point", "coordinates": [9.0, 53.0]}) is None


class TestBuildGetFeature:
    def test_results_request(self):
        body = build_get_feature("staatliche_schulen", _NS, build_filter(["<a/>"]), 500)

        assert 'count="500" outputFormat="application/geo+json"' in body
        assert f'xmlns:app="{_NS}"' in body
        assert 'typeNames="app:staatliche_schulen" srsName="EPSG:4326"' in body
        assert "<fes:Filter><a/></fes:Filter>" in body

    def test_hits_request_has_no_count_or_geojson(self):
        body = build_get_feature("staatliche_schulen", _NS, "", 0, hits=True)

        assert 'resultType="hits"' in body
        assert "count=" not in body and "geo+json" not in body

    def test_several_clauses_are_anded(self):
        assert build_filter(["<a/>", "<b/>"]) == "<fes:Filter><fes:And><a/><b/></fes:And></fes:Filter>"
        assert build_filter([]) == ""


class _FakeWfsServer:
    """Answers DescribeFeatureType, the sample request, the filtered
    request and a hits request; records the POSTed bodies."""

    def __init__(self, filtered_features, sample_features=None, hits=None):
        self.posts: list[str] = []
        self.filtered_features = filtered_features
        self.sample_features = sample_features or filtered_features
        self.hits = hits

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params["REQUEST"] == "DescribeFeatureType"
            return httpx.Response(200, text=_XSD)
        body = request.content.decode()
        self.posts.append(body)
        if 'resultType="hits"' in body:
            return httpx.Response(200, text=f'<wfs:FeatureCollection numberMatched="{self.hits}" numberReturned="0"/>')
        features = self.filtered_features if "<fes:Filter>" in body else self.sample_features
        return httpx.Response(200, json={"type": "FeatureCollection", "features": features})


def _agent(server: _FakeWfsServer, llm_reply: str, limit: int = 500) -> FilterAgent:
    class _LLM:
        def generate(self, prompt, max_tokens=None, label="generate"):
            self.prompt = prompt
            return llm_reply

    client = httpx.Client(transport=httpx.MockTransport(server.handler))
    settings = GeodataSettings(filter_result_limit=limit)
    agent = FilterAgent(settings=settings, llm=_LLM(), http_client=client)
    return agent


class TestFilterAgentWfs:
    def test_attribute_and_intersect_filter(self):
        server = _FakeWfsServer(
            filtered_features=[_feature(schulname="Grundschule Hoheluft", kapitelbezeichnung="Grundschulen")],
            sample_features=[
                _feature(kapitelbezeichnung="Grundschulen", schulname="A"),
                _feature(kapitelbezeichnung="Gymnasien", schulname="B"),
            ],
        )
        agent = _agent(server, '[{"field": "kapitelbezeichnung", "operator": "eq", "value": "Grundschulen"}]')

        result = agent.filter("Grundschulen in Hoheluft-West", _SCHOOL_LAYER, _HOHELUFT_WEST)

        # the LLM saw the stored category values
        assert "kapitelbezeichnung (string) - Beispielwerte: Grundschulen, Gymnasien" in agent.llm.prompt
        filtered = server.posts[-1]
        assert f'xmlns:app="{_NS}"' in filtered
        assert "<fes:Intersects><fes:ValueReference>the_geom</fes:ValueReference><gml:Polygon" in filtered
        assert "<fes:Literal>Grundschulen</fes:Literal>" in filtered
        assert result.layer_id == "8712.1"
        assert [f["properties"]["schulname"] for f in result.features["features"]] == ["Grundschule Hoheluft"]
        assert result.label == "Hoheluft-West"
        assert result.conditions == [FilterCondition("kapitelbezeichnung", "eq", "Grundschulen")]
        # complete result, checked against the area -> exact total
        assert result.number_matched == 1

    def test_full_page_asks_for_the_total(self):
        server = _FakeWfsServer(filtered_features=[_feature(kapitelbezeichnung="Grundschulen")] * 2, hits=208)
        agent = _agent(server, '[{"field": "kapitelbezeichnung", "operator": "eq", "value": "Grundschulen"}]', limit=2)

        result = agent.filter("alle Grundschulen", _SCHOOL_LAYER)

        assert result.number_matched == 208
        assert 'resultType="hits"' in server.posts[-1]

    def test_hallucinated_field_dropped_and_nothing_left(self):
        server = _FakeWfsServer(filtered_features=[])
        agent = _agent(server, '[{"field": "gibt_es_nicht", "operator": "eq", "value": "x"}]')

        assert agent.filter("irgendwas", _SCHOOL_LAYER) is None

    def test_wfs_layer_without_feature_type_is_skipped(self):
        server = _FakeWfsServer(filtered_features=[])
        agent = _agent(server, "[]")
        layer = LayerMatch("Schulen", "8712", 0.5, typ="WFS", url="https://x")

        assert agent.filter("Schulen in Hoheluft-West", layer, _HOHELUFT_WEST) is None
        assert server.posts == []

    def test_schema_is_cached(self):
        server = _FakeWfsServer(filtered_features=[_feature(kapitelbezeichnung="Grundschulen")])
        agent = _agent(server, "[]")
        calls = []
        original = server.handler

        def counting(request):
            calls.append(request.method)
            return original(request)

        agent.wfs._client = httpx.Client(transport=httpx.MockTransport(counting))
        agent.filter("in Hoheluft-West", _SCHOOL_LAYER, _HOHELUFT_WEST)
        agent.filter("in Hoheluft-West", _SCHOOL_LAYER, _HOHELUFT_WEST)

        # one DescribeFeatureType + one sample, then only the two filtered requests
        assert calls.count("GET") == 1
        assert calls.count("POST") == 3


def test_settings_json_roundtrip_unaffected():
    # FilterResult features stay plain GeoJSON dicts
    server = _FakeWfsServer(filtered_features=[_feature(kapitelbezeichnung="Grundschulen")])
    result = _agent(server, "[]").filter("in Hoheluft-West", _SCHOOL_LAYER, _HOHELUFT_WEST)
    json.dumps(result.features)


class TestLayerNameFallback:
    def test_category_layer_name_added_when_llm_returns_nothing(self):
        server = _FakeWfsServer(
            filtered_features=[_feature(kapitelbezeichnung="Gymnasien")],
            sample_features=[_feature(kapitelbezeichnung="Grundschulen"), _feature(kapitelbezeichnung="Gymnasien")],
        )
        layer = LayerMatch(
            "Gymnasien", "8712.3", 1.0, typ="WFS", url="https://geodienste.hamburg.de/HH_WFS_Schulen",
            feature_type="staatliche_schulen",
        )

        result = _agent(server, "[]").filter("Gymnasien in Hoheluft-West", layer, _HOHELUFT_WEST)

        assert result.conditions == [FilterCondition("kapitelbezeichnung", "eq", "Gymnasien")]
        assert "<fes:Literal>Gymnasien</fes:Literal>" in server.posts[-1]

    def test_not_added_when_the_field_is_already_constrained_or_no_value_matches(self):
        server = _FakeWfsServer(
            filtered_features=[],
            sample_features=[_feature(kapitelbezeichnung="Grundschulen"), _feature(kapitelbezeichnung="Gymnasien")],
        )
        reply = '[{"field": "kapitelbezeichnung", "operator": "eq", "value": "Grundschulen"}]'

        result = _agent(server, reply).filter("Grundschulen", LayerMatch(
            "Gymnasien", "8712.3", 1.0, typ="WFS", url="https://x", feature_type="staatliche_schulen"), _HOHELUFT_WEST)
        assert result.conditions == [FilterCondition("kapitelbezeichnung", "eq", "Grundschulen")]

        result = _agent(server, "[]").filter("in Hoheluft-West", LayerMatch(
            "WFS Schulen", "8712", 1.0, typ="WFS", url="https://x", feature_type="staatliche_schulen"), _HOHELUFT_WEST)
        assert result.conditions == []


class TestExactAreaCheck:
    """The service is asked for the area's bounding rectangle; only what
    lies inside the real outline is kept."""

    # an L-shaped area: its bounding rectangle also covers (9.98, 53.58)
    _L_AREA = LocationMatch(
        name="L", latitude=53.58, longitude=9.96, confidence=1.0, kind="stadtteil",
        boundary={"type": "MultiPolygon", "coordinates": [[[[9.95, 53.57], [9.99, 53.57], [9.99, 53.575],
                                                            [9.96, 53.575], [9.96, 53.59], [9.95, 53.59], [9.95, 53.57]]]]},
    )

    def _point(self, lon, lat, name):
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": {"schulname": name}}

    def test_rectangle_query_then_outline_check(self):
        server = _FakeWfsServer(filtered_features=[self._point(9.955, 53.58, "inside"), self._point(9.98, 53.58, "corner")])

        result = _agent(server, "[]").filter("Schulen", _SCHOOL_LAYER, self._L_AREA)

        # the request carried the rectangle (5 corners), not the outline
        assert "<gml:posList>9.95 53.57 9.99 53.57 9.99 53.59 9.95 53.59 9.95 53.57</gml:posList>" in server.posts[-1]
        assert [f["properties"]["schulname"] for f in result.features["features"]] == ["inside"]
        assert result.number_matched == 1

    def test_total_unknown_when_cut_at_the_limit(self):
        server = _FakeWfsServer(filtered_features=[self._point(9.955, 53.58, "a"), self._point(9.98, 53.58, "b")], hits=900)

        result = _agent(server, "[]", limit=2).filter("Schulen", _SCHOOL_LAYER, self._L_AREA)

        assert len(result.features["features"]) == 1
        assert result.number_matched is None
