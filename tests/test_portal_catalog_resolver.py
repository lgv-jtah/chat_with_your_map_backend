"""Unit tests for ``PortalCatalogResolver`` against a mocked HTTP
transport - no real network access needed for these.
"""

import httpx

from cwym_agent.geodata.portal_catalog_resolver import PortalCatalogResolver, _extract_layer_ids
from cwym_agent.geodata.settings import GeodataSettings

# Mirrors the real shape of chat_with_your_map_portal/config.json's
# layerConfig section (a mix of single ids and grouped-layer id lists).
_SAMPLE_CONFIG_JSON = {
    "layerConfig": {
        "baselayer": {
            "elements": [
                {"id": "453", "visibility": True},
                {"id": ["717", "718", "719"], "name": "Geobasiskarten (farbig)"},
            ]
        },
        "subjectlayer": {
            "elements": [
                {"id": "27764", "name": "StadtRAD-Stationen Hamburg"},
                {"id": ["3302", "4561"], "name": "WMS-Gruppe"},
            ]
        },
    }
}


class TestExtractLayerIds:
    def test_flattens_single_and_grouped_ids(self):
        ids = _extract_layer_ids(_SAMPLE_CONFIG_JSON)

        assert ids == {"453", "717", "718", "719", "27764", "3302", "4561"}

    def test_missing_layer_config_yields_empty_set(self):
        assert _extract_layer_ids({}) == set()

    def test_missing_sections_yield_empty_set(self):
        assert _extract_layer_ids({"layerConfig": {}}) == set()


def _resolver(handler, **settings_kwargs) -> PortalCatalogResolver:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return PortalCatalogResolver(GeodataSettings(**settings_kwargs), http_client=http_client)


class TestGetAllowedLayerIds:
    def test_no_url_means_unscoped(self):
        resolver = _resolver(lambda request: httpx.Response(200, json=_SAMPLE_CONFIG_JSON))

        assert resolver.get_allowed_layer_ids(None) is None
        assert resolver.get_allowed_layer_ids("") is None

    def test_fetches_and_extracts_ids(self):
        resolver = _resolver(lambda request: httpx.Response(200, json=_SAMPLE_CONFIG_JSON))

        result = resolver.get_allowed_layer_ids("https://portal.example/config.json")

        assert result == {"453", "717", "718", "719", "27764", "3302", "4561"}

    def test_caches_result_within_ttl(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200, json=_SAMPLE_CONFIG_JSON)

        resolver = _resolver(handler, portal_catalog_cache_ttl=600.0)

        resolver.get_allowed_layer_ids("https://portal.example/config.json")
        resolver.get_allowed_layer_ids("https://portal.example/config.json")

        assert calls["count"] == 1

    def test_refetches_after_ttl_expires(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200, json=_SAMPLE_CONFIG_JSON)

        resolver = _resolver(handler, portal_catalog_cache_ttl=0.0)

        resolver.get_allowed_layer_ids("https://portal.example/config.json")
        resolver.get_allowed_layer_ids("https://portal.example/config.json")

        assert calls["count"] == 2

    def test_caches_separately_per_url(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200, json=_SAMPLE_CONFIG_JSON)

        resolver = _resolver(handler)

        resolver.get_allowed_layer_ids("https://portal-a.example/config.json")
        resolver.get_allowed_layer_ids("https://portal-b.example/config.json")

        assert calls["count"] == 2

    def test_http_error_falls_back_to_none(self):
        resolver = _resolver(lambda request: httpx.Response(404))

        assert resolver.get_allowed_layer_ids("https://portal.example/config.json") is None

    def test_malformed_json_falls_back_to_none(self):
        resolver = _resolver(lambda request: httpx.Response(200, content=b"not json"))

        assert resolver.get_allowed_layer_ids("https://portal.example/config.json") is None

    def test_network_error_falls_back_to_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated network failure", request=request)

        resolver = _resolver(handler)

        assert resolver.get_allowed_layer_ids("https://portal.example/config.json") is None
