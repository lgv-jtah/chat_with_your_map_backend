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


class _RecordingClient:
    """A minimal httpx.Client stand-in that just records it was used -
    real TLS verification behavior (does verify=False actually let a
    self-signed cert through?) is confirmed against a real self-signed
    HTTPS server, not mocked here; this only tests which client
    PortalCatalogResolver *picks*."""

    def __init__(self, response: httpx.Response):
        self.response = response
        self.calls: list[str] = []

    def get(self, url: str) -> httpx.Response:
        self.calls.append(url)
        self.response.request = httpx.Request("GET", url)
        return self.response


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


class TestSsrfGuard:
    """A rejected URL must never actually be fetched - the handler raises
    if called, so any of these tests would fail loudly (not just return
    None for an unrelated reason) if the guard let the request through."""

    def _guarded_resolver(self, **settings_kwargs) -> PortalCatalogResolver:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"request should have been rejected before fetching: {request.url}")

        return _resolver(handler, **settings_kwargs)

    def test_rejects_cloud_metadata_ip_literal(self):
        resolver = self._guarded_resolver()

        assert resolver.get_allowed_layer_ids("http://169.254.169.254/config.json") is None

    def test_rejects_private_ip_literal(self):
        resolver = self._guarded_resolver()

        assert resolver.get_allowed_layer_ids("http://10.0.0.5/config.json") is None

    def test_loopback_allowed_by_default(self):
        """This project's only real deployment today runs frontend and
        backend on the same host - the frontend's own resolved
        config.json URL legitimately is loopback in that case, so
        rejecting it by default would silently disable scoping for the
        one setup that actually exists (confirmed live)."""

        resolver = _resolver(lambda request: httpx.Response(200, json=_SAMPLE_CONFIG_JSON))

        assert resolver.get_allowed_layer_ids("http://127.0.0.1/config.json") is not None
        assert resolver.get_allowed_layer_ids("http://[::1]/config.json") is not None
        assert resolver.get_allowed_layer_ids("http://localhost/config.json") is not None

    def test_loopback_rejected_when_disabled(self):
        resolver = self._guarded_resolver(portal_catalog_allow_loopback=False)

        assert resolver.get_allowed_layer_ids("http://127.0.0.1/config.json") is None
        assert resolver.get_allowed_layer_ids("http://[::1]/config.json") is None
        assert resolver.get_allowed_layer_ids("http://localhost/config.json") is None

    def test_rejects_non_http_scheme(self):
        resolver = self._guarded_resolver()

        assert resolver.get_allowed_layer_ids("file:///etc/passwd") is None

    def test_rejects_credentials_in_url(self):
        resolver = self._guarded_resolver()

        assert resolver.get_allowed_layer_ids("https://user:pass@portal.example/config.json") is None

    def test_allows_public_hostname_by_default(self):
        resolver = _resolver(lambda request: httpx.Response(200, json=_SAMPLE_CONFIG_JSON))

        assert resolver.get_allowed_layer_ids("https://portal.example/config.json") is not None

    def test_allowlist_rejects_host_not_in_it(self):
        resolver = self._guarded_resolver(portal_catalog_allowed_hosts=frozenset({"other.example"}))

        assert resolver.get_allowed_layer_ids("https://portal.example/config.json") is None

    def test_allowlist_permits_listed_host(self):
        resolver = _resolver(
            lambda request: httpx.Response(200, json=_SAMPLE_CONFIG_JSON),
            portal_catalog_allowed_hosts=frozenset({"portal.example"}),
        )

        assert resolver.get_allowed_layer_ids("https://portal.example/config.json") is not None

    def test_rejects_response_over_content_length_cap(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-length": "999999999"}, json=_SAMPLE_CONFIG_JSON)

        resolver = _resolver(handler, portal_catalog_max_response_bytes=1000)

        assert resolver.get_allowed_layer_ids("https://portal.example/config.json") is None

    def test_rejects_response_over_actual_size_cap(self):
        oversized = {"layerConfig": {"subjectlayer": {"elements": [{"id": "x" * 5000}]}}}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=oversized)

        resolver = _resolver(handler, portal_catalog_max_response_bytes=1000)

        assert resolver.get_allowed_layer_ids("https://portal.example/config.json") is None


class TestLoopbackUsesNonVerifyingClient:
    """webpack-dev-server's devServer.https is hardcoded true in this
    project - both `npm start` and `npm run start:ssl` serve over HTTPS
    with a self-signed certificate, confirmed live: fetching such a URL
    with a certificate-verifying client raises SSLCertVerificationError,
    silently falling back to unscoped search with nothing surfaced to
    the frontend. These tests only confirm PortalCatalogResolver picks
    the right client per host - the actual "does verify=False let a
    self-signed cert through" behavior is confirmed against a real
    self-signed HTTPS server, not mockable here."""

    def test_loopback_url_uses_the_loopback_client(self):
        strict = _RecordingClient(httpx.Response(200, json=_SAMPLE_CONFIG_JSON))
        loopback = _RecordingClient(httpx.Response(200, json=_SAMPLE_CONFIG_JSON))
        resolver = PortalCatalogResolver(http_client=strict, loopback_http_client=loopback)

        resolver.get_allowed_layer_ids("https://localhost:9001/config.json")

        assert loopback.calls == ["https://localhost:9001/config.json"]
        assert strict.calls == []

    def test_non_loopback_url_uses_the_strict_client(self):
        strict = _RecordingClient(httpx.Response(200, json=_SAMPLE_CONFIG_JSON))
        loopback = _RecordingClient(httpx.Response(200, json=_SAMPLE_CONFIG_JSON))
        resolver = PortalCatalogResolver(http_client=strict, loopback_http_client=loopback)

        resolver.get_allowed_layer_ids("https://portal.example/config.json")

        assert strict.calls == ["https://portal.example/config.json"]
        assert loopback.calls == []

    def test_loopback_disabled_never_calls_either_client(self):
        strict = _RecordingClient(httpx.Response(200, json=_SAMPLE_CONFIG_JSON))
        loopback = _RecordingClient(httpx.Response(200, json=_SAMPLE_CONFIG_JSON))
        resolver = PortalCatalogResolver(
            GeodataSettings(portal_catalog_allow_loopback=False),
            http_client=strict,
            loopback_http_client=loopback,
        )

        result = resolver.get_allowed_layer_ids("https://localhost:9001/config.json")

        assert result is None
        assert strict.calls == []
        assert loopback.calls == []
