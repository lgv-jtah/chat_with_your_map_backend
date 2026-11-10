"""Unit tests for ``OgcApiFeaturesBoundaries`` (built from the Hamburg
profile) and its use in ``LocationResolver``."""

import json

import httpx

from cwym_agent.city_profile import load_profile
from cwym_agent.geodata.adapters import build_boundary_source, build_geocoder
from cwym_agent.geodata.locations import LocationResolver
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import LocationMatch

_PROFILE = load_profile("hamburg")
_OUTLINE = {"type": "MultiPolygon", "coordinates": [[[[9.95, 53.57], [9.99, 53.57], [9.97, 53.6], [9.95, 53.57]]]]}


def _handler(requests: list):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = request.url.params
        if "addr_search" in params.get("source", ""):
            return httpx.Response(200, json={"hits": {"hits": [{
                "_score": 60.0,
                "_source": {"geometry": {"type": "Point", "coordinates": [9.97, 53.58]},
                            "properties": {"type": "County", "searchfield": "Hamburg-Harburg",
                                           "geographicextent": {"type": "Polygon", "coordinates": [[[9.9, 53.5], [10.0, 53.5], [10.0, 53.6], [9.9, 53.6], [9.9, 53.5]]]}}},
            }]}})
        assert request.url.path.startswith("/datasets/v1/verwaltungsgrenzen/collections/bezirke/items")
        if params.get("skipGeometry") == "true":
            return httpx.Response(200, json={"features": [{"properties": {"bezirk_name": "Harburg"}}, {"properties": {"bezirk_name": "Altona"}}]})
        if "filter" in params:
            assert params["filter"] == "bezirk_name='Harburg'"
            return httpx.Response(200, json={"features": [{"geometry": _OUTLINE, "properties": {}}]})
        return httpx.Response(404)
    return handle


def _client(requests):
    return httpx.Client(transport=httpx.MockTransport(_handler(requests)))


def _boundaries(client: httpx.Client):
    return build_boundary_source(_PROFILE.boundaries, GeodataSettings(), client)


class TestOgcApiFeaturesBoundaries:
    def test_outline_by_name_with_stripped_prefix(self):
        requests = []
        boundaries = _boundaries(_client(requests))

        outline = boundaries.outline("Hamburg-Harburg", "bezirk")

        # simplify may turn a one-part MultiPolygon into a Polygon
        assert outline["type"] in ("Polygon", "MultiPolygon")
        json.dumps(outline)  # plain JSON (lists, not tuples)
        assert boundaries.outline("Hamburg-Harburg", "bezirk") == outline
        assert len(requests) == 2  # name index + outline, then cached

    def test_unknown_or_other_kinds(self):
        requests = []
        boundaries = _boundaries(_client(requests))

        assert boundaries.outline("Gibtsnicht", "bezirk") is None
        assert boundaries.outline("Mönckebergstraße", "address") is None
        assert len(requests) == 1  # only the bezirk name index; "address" has no collection

    def test_service_down_means_none(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))

        assert _boundaries(client).outline("Altona", "bezirk") is None


class _FixedGeocoder:
    def __init__(self, match):
        self.match = match

    def search(self, query):
        return self.match


class _NoBoundaries:
    def outline(self, name, kind):
        raise AssertionError("must not be asked")


class TestLocationResolver:
    def test_rectangle_replaced_by_official_outline(self):
        client = _client([])
        resolver = LocationResolver(
            build_geocoder(_PROFILE.geocoder, GeodataSettings(), client), _boundaries(client)
        )

        match = resolver.resolve("Harburg")

        assert match.kind == "bezirk"
        assert match.boundary["type"] in ("Polygon", "MultiPolygon")
        assert match.boundary["coordinates"][0][0] == [9.95, 53.57]
        assert match.extent == (9.95, 53.57, 9.99, 53.6)

    def test_hit_without_kind_is_not_looked_up(self):
        street = LocationMatch("Mönckebergstraße", 53.55, 10.0, 0.5)

        assert LocationResolver(_FixedGeocoder(street), _NoBoundaries()).resolve("x") is street

    def test_no_hit(self):
        assert LocationResolver(_FixedGeocoder(None), _NoBoundaries()).resolve("x") is None
