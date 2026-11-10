"""Unit tests for ``HamburgAddrSearchGeocoder``, built from the Hamburg
profile, against a mocked HTTP transport."""

import json

import httpx

from cwym_agent.city_profile import load_profile
from cwym_agent.geodata.adapters import build_geocoder
from cwym_agent.geodata.adapters.hamburg_addr_search import HamburgAddrSearchGeocoder
from cwym_agent.geodata.settings import GeodataSettings

_PROFILE = load_profile("hamburg")


def _geocoder(http_client: httpx.Client) -> HamburgAddrSearchGeocoder:
    geocoder = build_geocoder(_PROFILE.geocoder, GeodataSettings(), http_client)
    assert isinstance(geocoder, HamburgAddrSearchGeocoder)
    return geocoder


def _hit(
    searchfield: str,
    lon: float,
    lat: float,
    type_: str,
    score: float,
    geographicextent: dict | None = None,
) -> dict:
    properties = {"type": type_, "searchfield": searchfield}
    if geographicextent is not None:
        properties["geographicextent"] = geographicextent

    return {
        "_score": score,
        "_source": {
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": properties,
        },
    }


def _client_for(hits: list[dict]) -> HamburgAddrSearchGeocoder:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": hits}})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return _geocoder(http_client)


class TestResolve:
    def test_no_hits_returns_none(self):
        resolver = _client_for([])
        assert resolver.search("nonsense query") is None

    def test_prefers_broad_type_over_higher_scoring_street(self):
        # Mirrors the real "Wandsbek" query: many higher-scoring Street
        # hits, but the district-level hit is what a user actually means.
        hits = [
            _hit("Wandsbeker Allee", 10.07, 53.58, "Street", 77.6),
            _hit("Wandsbeker Stieg", 10.03, 53.56, "Street", 77.6),
            _hit("Wandsbek", 10.11, 53.64, "District", 58.3),
            _hit("Wandsbek", 10.11, 53.64, "County", 58.3),
        ]
        resolver = _client_for(hits)

        result = resolver.search("Wandsbek")

        assert result is not None
        assert result.name == "Wandsbek"
        assert result.latitude == 53.64
        assert result.longitude == 10.11

    def test_exact_broad_match_wins_over_higher_scoring_partial_broad_match(self):
        hits = [
            _hit("Altona-Altstadt", 9.94, 53.55, "District", 57.9),
            _hit("Altona", 9.85, 53.59, "County", 57.7),
        ]
        resolver = _client_for(hits)

        result = resolver.search("Altona")

        assert result is not None
        assert result.name == "Altona"

    def test_falls_back_to_top_hit_when_no_broad_type_present(self):
        hits = [
            _hit("Mönckebergstraße", 10.0, 53.55, "Street", 50.0),
            _hit("Mönckebergstraße 7", 10.0, 53.55, "Address", 30.0),
        ]
        resolver = _client_for(hits)

        result = resolver.search("Mönckebergstraße")

        assert result is not None
        assert result.name == "Mönckebergstraße"

    def test_confidence_is_normalized_and_capped(self):
        hits = [_hit("Hamburg-Nord", 10.0, 53.6, "County", 250.0)]
        resolver = _client_for(hits)

        result = resolver.search("Hamburg-Nord")

        assert result is not None
        assert result.confidence == 1.0

    def test_district_hit_with_polygon_extent_populates_boundary(self):
        polygon = {
            "type": "Polygon",
            "coordinates": [[[10.0, 53.5], [10.1, 53.5], [10.1, 53.6], [10.0, 53.6], [10.0, 53.5]]],
            "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        }
        hits = [_hit("Wandsbek", 10.11, 53.64, "District", 58.3, geographicextent=polygon)]
        resolver = _client_for(hits)

        result = resolver.search("Wandsbek")

        assert result is not None
        assert result.boundary == {"type": "Polygon", "coordinates": polygon["coordinates"]}

    def test_district_type_hit_gets_stadtteil_kind(self):
        hits = [_hit("Ottensen", 9.93, 53.55, "District", 71.1)]
        resolver = _client_for(hits)

        result = resolver.search("Ottensen")

        assert result is not None
        assert result.kind == "stadtteil"

    def test_county_type_hit_gets_bezirk_kind(self):
        hits = [_hit("Altona", 9.85, 53.59, "County", 57.7)]
        resolver = _client_for(hits)

        result = resolver.search("Altona")

        assert result is not None
        assert result.kind == "bezirk"

    def test_address_type_hit_gets_address_kind(self):
        hits = [_hit("Mönckebergstraße 1", 10.0, 53.55, "Address", 41.1)]
        resolver = _client_for(hits)

        result = resolver.search("Mönckebergstraße 1")

        assert result is not None
        assert result.kind == "address"

    def test_street_type_hit_gets_no_kind(self):
        hits = [_hit("Mönckebergstraße", 10.0, 53.55, "Street", 50.0)]
        resolver = _client_for(hits)

        result = resolver.search("Mönckebergstraße")

        assert result is not None
        assert result.kind is None

    def test_district_hit_with_polygon_extent_also_populates_bbox_extent(self):
        polygon = {
            "type": "Polygon",
            "coordinates": [[[10.0, 53.5], [10.1, 53.5], [10.1, 53.6], [10.0, 53.6], [10.0, 53.5]]],
        }
        hits = [_hit("Wandsbek", 10.11, 53.64, "District", 58.3, geographicextent=polygon)]
        resolver = _client_for(hits)

        result = resolver.search("Wandsbek")

        assert result is not None
        assert result.extent == (10.0, 53.5, 10.1, 53.6)

    def test_multipolygon_extent_bbox_covers_all_parts(self):
        multipolygon = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[10.0, 53.5], [10.1, 53.5], [10.1, 53.6], [10.0, 53.6], [10.0, 53.5]]],
                [[[11.0, 54.0], [11.2, 54.0], [11.2, 54.2], [11.0, 54.2], [11.0, 54.0]]],
            ],
        }
        hits = [_hit("Multi", 10.5, 53.8, "District", 58.3, geographicextent=multipolygon)]
        resolver = _client_for(hits)

        result = resolver.search("Multi")

        assert result is not None
        assert result.extent == (10.0, 53.5, 11.2, 54.2)

    def test_street_hit_with_point_extent_leaves_boundary_none(self):
        point_extent = {"type": "Point", "coordinates": [10.0, 53.55]}
        hits = [_hit("Mönckebergstraße", 10.0, 53.55, "Street", 50.0, geographicextent=point_extent)]
        resolver = _client_for(hits)

        result = resolver.search("Mönckebergstraße")

        assert result is not None
        assert result.boundary is None

    def test_hit_without_geographicextent_leaves_boundary_none(self):
        hits = [_hit("Altona", 9.85, 53.59, "County", 57.7)]
        resolver = _client_for(hits)

        result = resolver.search("Altona")

        assert result is not None
        assert result.boundary is None
        assert result.extent is None

    def test_http_error_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        resolver = _geocoder(http_client)

        assert resolver.search("Wandsbek") is None

    def test_request_goes_to_the_profile_url(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url.copy_with(query=None))
            return httpx.Response(200, json={"hits": {"hits": []}})

        _geocoder(httpx.Client(transport=httpx.MockTransport(handler))).search("Wandsbek")

        assert captured["url"] == _PROFILE.geocoder.url

    def test_env_url_overrides_the_profile(self, monkeypatch):
        monkeypatch.setenv("CWYM_ADDRESS_SEARCH_URL", "https://mirror.example/addr_search")

        geocoder = build_geocoder(_PROFILE.geocoder, GeodataSettings(), httpx.Client())

        assert geocoder.url == "https://mirror.example/addr_search"

    def test_request_uses_source_envelope(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"hits": {"hits": []}})

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        resolver = _geocoder(http_client)

        resolver.search("Wandsbek")

        assert captured["params"]["source_content_type"] == "application/json"
        source = json.loads(captured["params"]["source"])
        assert source["id"] == "addr_search"
        assert source["params"]["query_str"] == "Wandsbek"


class TestAmbiguousAreas:
    """Hits mirror Hamburg's real answers (checked live)."""

    def test_two_districts_starting_with_the_query(self):
        resolver = _client_for(
            [
                _hit("Hoheluftchaussee", 9.97, 53.58, "Street", 70.0),
                _hit("Hoheluft-Ost", 9.976, 53.584, "District", 66.2),
                _hit("Hoheluft-West", 9.966, 53.579, "District", 60.0),
            ]
        )

        result = resolver.search("Hoheluft")

        assert result.name == "Hoheluft-Ost"
        assert result.alternatives == ["Hoheluft-West"]

    def test_exact_area_hit_is_not_ambiguous(self):
        resolver = _client_for(
            [
                _hit("Altona-Altstadt", 9.93, 53.55, "District", 70.0),
                _hit("Altona", 9.93, 53.56, "County", 65.0),
                _hit("Altona-Nord", 9.94, 53.57, "District", 60.0),
            ]
        )

        result = resolver.search("Altona")

        assert result.name == "Altona"
        assert result.alternatives == []

    def test_single_prefix_match_is_not_ambiguous(self):
        resolver = _client_for([_hit("Barmbek-Nord", 10.04, 53.6, "District", 60.0)])

        assert resolver.search("Barmbek").alternatives == []

    def test_streets_never_count(self):
        resolver = _client_for(
            [
                _hit("Hoheluft-Ost", 9.976, 53.584, "District", 66.2),
                _hit("Hoheluft-Weg", 9.97, 53.58, "Street", 80.0),
            ]
        )

        assert resolver.search("Hoheluft").alternatives == []
