"""Unit tests for ``AreaBoundaries`` and its use in ``AddressResolver``."""

import json

import httpx

from cwym_agent.geodata.address_resolver import AddressResolver
from cwym_agent.geodata.area_boundaries import AreaBoundaries
from cwym_agent.geodata.settings import GeodataSettings

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
        if params.get("skipGeometry") == "true":
            return httpx.Response(200, json={"features": [{"properties": {"bezirk_name": "Harburg"}}, {"properties": {"bezirk_name": "Altona"}}]})
        if "filter" in params:
            assert params["filter"] == "bezirk_name='Harburg'"
            return httpx.Response(200, json={"features": [{"geometry": _OUTLINE, "properties": {}}]})
        return httpx.Response(404)
    return handle


def _client(requests):
    return httpx.Client(transport=httpx.MockTransport(_handler(requests)))


class TestAreaBoundaries:
    def test_outline_by_name_with_hamburg_prefix(self):
        requests = []
        boundaries = AreaBoundaries(GeodataSettings(), _client(requests))

        outline = boundaries.outline("Hamburg-Harburg", "bezirk")

        # simplify may turn a one-part MultiPolygon into a Polygon
        assert outline["type"] in ("Polygon", "MultiPolygon")
        json.dumps(outline)  # plain JSON (lists, not tuples)
        assert boundaries.outline("Hamburg-Harburg", "bezirk") == outline
        assert len(requests) == 2  # name index + outline, then cached

    def test_unknown_or_other_kinds(self):
        boundaries = AreaBoundaries(GeodataSettings(), _client([]))

        assert boundaries.outline("Gibtsnicht", "bezirk") is None
        assert boundaries.outline("Mönckebergstraße", "address") is None

    def test_service_down_means_none(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))

        assert AreaBoundaries(GeodataSettings(), client).outline("Altona", "bezirk") is None


class TestAddressResolverUsesOutlines:
    def test_rectangle_replaced_by_official_outline(self):
        resolver = AddressResolver(GeodataSettings(), http_client=_client([]))

        match = resolver.resolve("Harburg")

        assert match.kind == "bezirk"
        assert match.boundary["type"] in ("Polygon", "MultiPolygon")
        assert match.boundary["coordinates"][0][0] == [9.95, 53.57]
        assert match.extent == (9.95, 53.57, 9.99, 53.6)
