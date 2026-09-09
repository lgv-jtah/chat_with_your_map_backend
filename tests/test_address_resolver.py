"""Unit tests for ``AddressResolver`` against a mocked HTTP transport."""

import json

import httpx

from cwym_agent.geodata.address_resolver import AddressResolver
from cwym_agent.geodata.settings import GeodataSettings


def _hit(searchfield: str, lon: float, lat: float, type_: str, score: float) -> dict:
    return {
        "_score": score,
        "_source": {
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"type": type_, "searchfield": searchfield},
        },
    }


def _client_for(hits: list[dict]) -> AddressResolver:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": hits}})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return AddressResolver(GeodataSettings(), http_client=http_client)


class TestResolve:
    def test_no_hits_returns_none(self):
        resolver = _client_for([])
        assert resolver.resolve("nonsense query") is None

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

        result = resolver.resolve("Wandsbek")

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

        result = resolver.resolve("Altona")

        assert result is not None
        assert result.name == "Altona"

    def test_falls_back_to_top_hit_when_no_broad_type_present(self):
        hits = [
            _hit("Mönckebergstraße", 10.0, 53.55, "Street", 50.0),
            _hit("Mönckebergstraße 7", 10.0, 53.55, "Address", 30.0),
        ]
        resolver = _client_for(hits)

        result = resolver.resolve("Mönckebergstraße")

        assert result is not None
        assert result.name == "Mönckebergstraße"

    def test_confidence_is_normalized_and_capped(self):
        hits = [_hit("Hamburg-Nord", 10.0, 53.6, "County", 250.0)]
        resolver = _client_for(hits)

        result = resolver.resolve("Hamburg-Nord")

        assert result is not None
        assert result.confidence == 1.0

    def test_http_error_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        resolver = AddressResolver(GeodataSettings(), http_client=http_client)

        assert resolver.resolve("Wandsbek") is None

    def test_request_uses_source_envelope(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"hits": {"hits": []}})

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        resolver = AddressResolver(GeodataSettings(), http_client=http_client)

        resolver.resolve("Wandsbek")

        assert captured["params"]["source_content_type"] == "application/json"
        source = json.loads(captured["params"]["source"])
        assert source["id"] == "addr_search"
        assert source["params"]["query_str"] == "Wandsbek"
