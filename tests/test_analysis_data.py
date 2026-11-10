"""Tier 1 data retrieval (CHAT-11, architecture 14.4 rule 1): complete
sets or an honest "incomplete", never a part passed off as the whole."""

import json
import urllib.parse

import httpx
import pytest
from fixtures.datasets import BUILDINGS, FIXTURES

from cwym_agent.city_profile import AttributeConfig
from cwym_agent.geodata.analysis_data import (
    DatasetError,
    FileSource,
    OafSource,
    WfsSource,
    condition_matches,
    load_feature_collection,
)
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import FilterCondition as C
from cwym_agent.geodata.wfs_filter import WfsClient, WfsSchema

FEATURES = load_feature_collection(FIXTURES / "buildings.geojson")
ATTRS = {name: AttributeConfig.model_validate(a) for name, a in BUILDINGS["attributes"].items()}
# The fixture's first row of buildings (B01-B04) lies in this rectangle.
FIRST_ROW = {"type": "Polygon", "coordinates": [[[9.9995, 53.5995], [10.0075, 53.5995], [10.0075, 53.6015], [9.9995, 53.6015], [9.9995, 53.5995]]]}


def ids(features):
    return [f["properties"]["gebid"] for f in features]


class TestConditionMatches:
    @pytest.mark.parametrize(
        "properties, condition, attribute, expected",
        [
            ({"k": "F"}, C("k", "gt", "E"), "energieeffizienzklasse", True),  # worse than E, by order
            ({"k": "A+"}, C("k", "lt", "A"), "energieeffizienzklasse", True),
            ({"k": "C"}, C("k", "gte", "E"), "energieeffizienzklasse", False),
            ({"k": "1963"}, C("k", "gt", 1960), "baujahr", True),  # number stored as text
            ({"k": "94,3"}, C("k", "gte", "90"), "pv_leistung", True),
            ({"k": "Wohnbau"}, C("k", "eq", "wohnbau"), "eigentuemer", True),
            ({"k": "Wohnbau"}, C("k", "ne", "Stadt"), "eigentuemer", True),
            ({"k": "Wohnbau"}, C("k", "contains", "bau"), "eigentuemer", True),
            ({"k": "Stadt"}, C("k", "eq", ["Kirche", "Stadt"]), "eigentuemer", True),
            ({}, C("k", "ne", "Stadt"), "eigentuemer", False),  # missing never matches
            ({"k": ""}, C("k", "eq", ""), "eigentuemer", False),
            ({"k": "Wohnbau"}, C("k", "gt", "Stadt"), "eigentuemer", False),  # categories don't order
        ],
    )
    def test_by_declared_type(self, properties, condition, attribute, expected):
        assert condition_matches(properties, condition, ATTRS[attribute]) is expected


class TestFileSource:
    def test_conditions_and_area(self):
        source = FileSource(FEATURES, ATTRS)

        owned = source.fetch([C("eigentuemer", "eq", "Wohnbau")], None, True)
        in_row = source.fetch([C("eigentuemer", "eq", "Wohnbau")], FIRST_ROW, True)

        assert owned.complete and owned.total == 7
        assert ids(in_row.features) == ["B01", "B02", "B03"]

    def test_bad_file(self, tmp_path):
        bad = tmp_path / "x.geojson"
        bad.write_text(json.dumps({"type": "Feature"}))

        with pytest.raises(DatasetError):
            load_feature_collection(bad)


# --- OGC API Features ---------------------------------------------------------------

_SETTINGS = GeodataSettings(analysis_page_size=5, analysis_max_features=10)
_QUERYABLES = {"eigentuemer": {"type": "string"}, "baujahr": {"type": "string"}, "geom": {"x-ogc-role": "primary-geometry"}}


class _FakeOaf:
    """Pages FEATURES by `limit`, with `next` links (or `offset` if
    links=False)."""

    def __init__(self, features=FEATURES, links=True, number_matched=True):
        self.features, self.links, self.number_matched = features, links, number_matched
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        limit, offset = int(params.get("limit", 10)), int(params.get("offset", 0))
        page = self.features[offset : offset + limit]
        body = {"type": "FeatureCollection", "features": page}
        if self.number_matched:
            body["numberMatched"] = len(self.features)
        if self.links and offset + limit < len(self.features):
            query = urllib.parse.urlencode({**params, "offset": offset + limit})
            body["links"] = [{"rel": "next", "href": f"https://api.example/collections/b/items?{query}"}]
        return httpx.Response(200, json=body)


def _oaf(server, settings=_SETTINGS):
    client = httpx.Client(transport=httpx.MockTransport(server.handler))
    return OafSource("https://api.example", "b", _QUERYABLES, "geom", ATTRS, client, settings)


class TestOafSource:
    def test_count_uses_the_servers_total(self):
        server = _FakeOaf()

        result = _oaf(server).fetch([C("eigentuemer", "eq", "Wohnbau")], None, need_features=False)

        assert (result.total, result.complete, result.features) == (12, True, None)
        assert len(server.requests) == 1 and server.requests[0]["limit"] == "1"
        assert server.requests[0]["filter"] == "eigentuemer = 'Wohnbau'"

    @pytest.mark.parametrize("links", [True, False])
    def test_pages_through_everything(self, links):
        server = _FakeOaf(links=links)

        result = _oaf(server, GeodataSettings(analysis_page_size=5, analysis_max_features=100)).fetch([], None, True)

        assert result.complete and ids(result.features) == ids(FEATURES)
        assert len(server.requests) == 3

    def test_above_the_cap_is_incomplete_not_partial(self):
        result = _oaf(_FakeOaf()).fetch([], None, True)  # 12 > cap 10

        assert (result.complete, result.reason, result.features, result.total) == (False, "too_many", None, None)

    def test_cap_also_holds_without_number_matched(self):
        result = _oaf(_FakeOaf(number_matched=False)).fetch([], None, True)

        assert (result.complete, result.reason) == (False, "too_many")

    def test_ordinal_and_text_number_ranges_are_applied_here(self):
        server = _FakeOaf()
        settings = GeodataSettings(analysis_page_size=50, analysis_max_features=100)

        result = _oaf(server, settings).fetch(
            [C("eigentuemer", "eq", "Wohnbau"), C("energieeffizienzklasse", "gte", "F"), C("baujahr", "lt", 1960)],
            None,
            need_features=False,
        )

        # only the equality went to the server; the counting happened here
        assert server.requests[0]["filter"] == "eigentuemer = 'Wohnbau'"
        # the fake ignores the server filter: what comes back is the local
        # conditions alone - class F or worse, built before 1960
        assert ids(result.features) == ["B01", "B02", "B06", "B11"]

    def test_area_is_checked_against_the_outline(self):
        server = _FakeOaf()
        settings = GeodataSettings(analysis_page_size=50, analysis_max_features=100)

        result = _oaf(server, settings).fetch([], FIRST_ROW, need_features=False)

        assert server.requests[0]["filter"].startswith("S_INTERSECTS(geom, POLYGON")
        assert result.total == 4  # never the rectangle's total

    def test_service_error(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        source = OafSource("https://api.example", "b", _QUERYABLES, "geom", ATTRS, client, _SETTINGS)

        assert source.fetch([], None, True).reason == "service_error"


# --- WFS -------------------------------------------------------------------------------

_SCHEMA = WfsSchema(namespace="http://example/app", geometry_field="geom", fields={"eigentuemer": "string", "geschosse": "integer"})


class _FakeWfs:
    def __init__(self, features=FEATURES):
        self.features = features
        self.bodies: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        self.bodies.append(body)
        if 'resultType="hits"' in body:
            return httpx.Response(200, text=f'<wfs:FeatureCollection numberMatched="{len(self.features)}"/>')
        count = int(body.split('count="')[1].split('"')[0])
        start = int(body.split('startIndex="')[1].split('"')[0]) if "startIndex" in body else 0
        return httpx.Response(200, json={"type": "FeatureCollection", "features": self.features[start : start + count]})


def _wfs(server, settings):
    client = httpx.Client(transport=httpx.MockTransport(server.handler))
    return WfsSource("https://wfs.example?SERVICE=WFS", "gebaeude", _SCHEMA, ATTRS, WfsClient(settings, client), settings)


class TestWfsSource:
    def test_count_uses_hits(self):
        server = _FakeWfs()

        result = _wfs(server, _SETTINGS).fetch([], None, need_features=False)

        assert result.total == 12 and len(server.bodies) == 1

    def test_pages_with_start_index(self):
        server = _FakeWfs()
        settings = GeodataSettings(analysis_page_size=5, analysis_max_features=100)

        result = _wfs(server, settings).fetch([C("geschosse", "gte", 4)], None, True)

        pages = [b for b in server.bodies if "hits" not in b]
        assert [('startIndex="5"' in b, 'startIndex="10"' in b) for b in pages] == [(False, False), (True, False), (False, True)]
        assert "PropertyIsGreaterThanOrEqualTo" in pages[0]  # numeric server field: sent to the server
        assert result.complete and len(result.features) == 12  # the fake ignores filters

    def test_above_the_cap_via_hits(self):
        result = _wfs(_FakeWfs(), _SETTINGS).fetch([], None, True)

        assert (result.complete, result.reason) == (False, "too_many")
