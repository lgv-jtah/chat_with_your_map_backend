"""Unit tests for ``filter_agent.py`` - CQL2 compilation is tested as
pure functions (no model/network needed), and ``FilterAgent.filter()``'s
orchestration against a mocked HTTP transport + a fake LLM. Real
end-to-end scoping quality (does S_INTERSECTS actually narrow results
against the real API) is covered by the opt-in live test in
test_geodata_live.py, same split as layer_search.py/test_layer_search.py.
"""

import json

import httpx

from cwym_agent.geodata.filter_agent import (
    FilterAgent,
    QueryablesCache,
    _boundary_to_wkt,
    _condition_to_cql2,
)
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import FilterCondition, LayerMatch, LocationMatch

_QUERYABLES = {
    "properties": {
        "name": {"title": "name", "type": "string"},
        "betten": {"title": "betten", "type": "integer"},
        "geom": {"title": "geom", "x-ogc-role": "primary-geometry", "format": "geometry-point"},
    }
}

_OAF_LAYER = LayerMatch(
    name="Krankenhäuser",
    layer_id="27714",
    confidence=0.9,
    typ="OAF",
    url="https://api.hamburg.de/datasets/v1/krankenhaeuser",
    collection="gesundheit_krankenhaeuser",
)

_WANDSBEK_BOUNDARY = {
    "type": "Polygon",
    "coordinates": [[[10.0, 53.5], [10.1, 53.5], [10.1, 53.6], [10.0, 53.6], [10.0, 53.5]]],
}


class TestConditionToCql2:
    def test_eq_string_is_quoted_and_escaped(self):
        condition = FilterCondition(field="name", operator="eq", value="O'Brien")
        assert _condition_to_cql2(condition) == "name = 'O''Brien'"

    def test_numeric_operators_are_not_quoted(self):
        assert _condition_to_cql2(FilterCondition("betten", "gt", 100)) == "betten > 100"
        assert _condition_to_cql2(FilterCondition("betten", "gte", 100)) == "betten >= 100"
        assert _condition_to_cql2(FilterCondition("betten", "lt", 100)) == "betten < 100"
        assert _condition_to_cql2(FilterCondition("betten", "lte", 100)) == "betten <= 100"
        assert _condition_to_cql2(FilterCondition("betten", "ne", 100)) == "betten <> 100"

    def test_contains_compiles_to_like_wildcard(self):
        condition = FilterCondition(field="name", operator="contains", value="Klinik")
        assert _condition_to_cql2(condition) == "name LIKE '%Klinik%'"

    def test_boolean_value(self):
        assert _condition_to_cql2(FilterCondition("aktiv", "eq", True)) == "aktiv = true"

    def test_unknown_operator_returns_none(self):
        assert _condition_to_cql2(FilterCondition("name", "regex", "x")) is None


class TestBoundaryToWkt:
    def test_polygon(self):
        wkt = _boundary_to_wkt(_WANDSBEK_BOUNDARY)
        assert wkt == "POLYGON((10.0 53.5, 10.1 53.5, 10.1 53.6, 10.0 53.6, 10.0 53.5))"

    def test_multipolygon(self):
        boundary = {
            "type": "MultiPolygon",
            "coordinates": [[[[10.0, 53.5], [10.1, 53.5], [10.0, 53.5]]]],
        }
        assert _boundary_to_wkt(boundary) == "MULTIPOLYGON(((10.0 53.5, 10.1 53.5, 10.0 53.5)))"

    def test_unsupported_type_returns_none(self):
        assert _boundary_to_wkt({"type": "Point", "coordinates": [10.0, 53.5]}) is None

    def test_missing_coordinates_returns_none(self):
        assert _boundary_to_wkt({"type": "Polygon"}) is None


class TestQueryablesCache:
    def test_fetches_and_caches(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200, json=_QUERYABLES)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cache = QueryablesCache(GeodataSettings(), http_client=client)

        first = cache.get("https://api.example/x", "coll")
        second = cache.get("https://api.example/x", "coll")

        assert first == _QUERYABLES["properties"]
        assert second == _QUERYABLES["properties"]
        assert calls["count"] == 1

    def test_fetch_failure_returns_none(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        cache = QueryablesCache(GeodataSettings(), http_client=client)

        assert cache.get("https://api.example/x", "coll") is None

    def test_geometry_field_finds_primary_geometry_role(self):
        assert QueryablesCache.geometry_field(_QUERYABLES["properties"]) == "geom"

    def test_geometry_field_none_when_absent(self):
        assert QueryablesCache.geometry_field({"name": {"type": "string"}}) is None


def _mock_llm(reply: str):
    class _FakeLlm:
        def generate(self, prompt: str, max_tokens=None) -> str:
            return reply

    return _FakeLlm()


def _oaf_handler(queryables_response=None, items_response=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if "queryables" in str(request.url):
            return httpx.Response(200, json=queryables_response or _QUERYABLES)
        return httpx.Response(
            200,
            json=items_response
            or {"type": "FeatureCollection", "numberMatched": 0, "features": []},
        )

    return handler


class TestFilterAgentFilter:
    def test_non_oaf_layer_returns_none(self):
        layer = LayerMatch(name="X", layer_id="1", confidence=0.9, typ="WMS")
        agent = FilterAgent(llm=_mock_llm("[]"))

        assert agent.filter("query", layer) is None

    def test_no_layer_returns_none(self):
        agent = FilterAgent(llm=_mock_llm("[]"))
        assert agent.filter("query", None) is None

    def test_attribute_condition_builds_correct_filter_and_returns_features(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "queryables" in str(request.url):
                return httpx.Response(200, json=_QUERYABLES)
            captured["filter"] = request.url.params.get("filter")
            captured["filter-lang"] = request.url.params.get("filter-lang")
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "numberMatched": 2,
                    "features": [{"type": "Feature", "properties": {"betten": 150}}],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reply = json.dumps([{"field": "betten", "operator": "gt", "value": 100}])
        agent = FilterAgent(llm=_mock_llm(reply), http_client=client)

        result = agent.filter("Krankenhäuser mit mehr als 100 Betten", _OAF_LAYER)

        assert result is not None
        assert result.layer_id == "27714"
        assert result.number_matched == 2
        assert len(result.features["features"]) == 1
        assert captured["filter"] == "betten > 100"
        assert captured["filter-lang"] == "cql2-text"

    def test_spatial_predicate_added_when_location_has_boundary(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "queryables" in str(request.url):
                return httpx.Response(200, json=_QUERYABLES)
            captured["filter"] = request.url.params.get("filter")
            return httpx.Response(200, json={"type": "FeatureCollection", "numberMatched": 1, "features": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        agent = FilterAgent(llm=_mock_llm("[]"), http_client=client)
        location = LocationMatch("Wandsbek", 53.6, 10.1, 0.9, boundary=_WANDSBEK_BOUNDARY)

        result = agent.filter("Krankenhäuser in Wandsbek", _OAF_LAYER, location)

        assert result is not None
        assert captured["filter"].startswith("S_INTERSECTS(geom, POLYGON(")
        assert result.label == "Wandsbek"

    def test_spatial_and_attribute_predicates_combined_with_and(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "queryables" in str(request.url):
                return httpx.Response(200, json=_QUERYABLES)
            captured["filter"] = request.url.params.get("filter")
            return httpx.Response(200, json={"type": "FeatureCollection", "numberMatched": 0, "features": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reply = json.dumps([{"field": "betten", "operator": "gt", "value": 50}])
        agent = FilterAgent(llm=_mock_llm(reply), http_client=client)
        location = LocationMatch("Wandsbek", 53.6, 10.1, 0.9, boundary=_WANDSBEK_BOUNDARY)

        agent.filter("Krankenhäuser mit mehr als 50 Betten in Wandsbek", _OAF_LAYER, location)

        assert " AND " in captured["filter"]
        assert captured["filter"].startswith("S_INTERSECTS")
        assert captured["filter"].endswith("betten > 50")

    def test_no_conditions_and_no_boundary_returns_none_without_querying_items(self):
        item_calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "queryables" in str(request.url):
                return httpx.Response(200, json=_QUERYABLES)
            item_calls["count"] += 1
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        agent = FilterAgent(llm=_mock_llm("[]"), http_client=client)

        result = agent.filter("Krankenhäuser", _OAF_LAYER)

        assert result is None
        assert item_calls["count"] == 0

    def test_llm_hallucinated_field_is_dropped(self):
        client = httpx.Client(transport=httpx.MockTransport(_oaf_handler()))
        reply = json.dumps([{"field": "nicht_existent", "operator": "eq", "value": "x"}])
        agent = FilterAgent(llm=_mock_llm(reply), http_client=client)

        # No valid condition and no boundary -> nothing to filter by.
        assert agent.filter("query", _OAF_LAYER) is None

    def test_unparseable_llm_reply_falls_back_to_empty_conditions(self):
        client = httpx.Client(transport=httpx.MockTransport(_oaf_handler()))
        agent = FilterAgent(llm=_mock_llm("nicht JSON"), http_client=client)

        assert agent.filter("query", _OAF_LAYER) is None

    def test_items_query_failure_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "queryables" in str(request.url):
                return httpx.Response(200, json=_QUERYABLES)
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reply = json.dumps([{"field": "betten", "operator": "gt", "value": 1}])
        agent = FilterAgent(llm=_mock_llm(reply), http_client=client)

        assert agent.filter("query", _OAF_LAYER) is None
