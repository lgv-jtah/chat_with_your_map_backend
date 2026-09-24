"""Unit tests for ``PortalCatalogResolver`` against a mocked async HTTP
transport - no real network access needed for these. ``httpx.AsyncClient``
+ ``MockTransport`` requires an async handler function (confirmed
directly against httpx before writing these), so every handler here is
``async def``; test bodies drive the resolver's async methods via
``asyncio.run`` rather than pulling in pytest-asyncio for a handful of
tests.
"""

import asyncio

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

# Mirrors the real shape of services-internet.json: a flat list of
# per-layer metadata objects, confirmed live against the actual file.
_SAMPLE_SERVICES_JSON = [
    {"id": "453", "typ": "WMS", "name": "Digitale Orthophotos"},
    {"id": "717", "typ": "WMS", "name": "Geobasiskarten Teil 1"},
    {"id": "718", "typ": "WMS", "name": "Geobasiskarten Teil 2"},
    {"id": "719", "typ": "WMS", "name": "Geobasiskarten Teil 3"},
    {
        "id": "27764",
        "typ": "OAF",
        "name": "StadtRAD-Stationen Hamburg",
        "url": "https://api.hamburg.de/datasets/v1/stadtrad",
        "collection": "stadtrad_stationen",
    },
    {"id": "3302", "typ": "WMS", "name": "WMS-Gruppe A"},
    {"id": "4561", "typ": "WMS", "name": "WMS-Gruppe B"},
]


class TestExtractLayerIds:
    def test_flattens_single_and_grouped_ids(self):
        ids = _extract_layer_ids(_SAMPLE_CONFIG_JSON)

        assert ids == {"453", "717", "718", "719", "27764", "3302", "4561"}

    def test_missing_layer_config_yields_empty_set(self):
        assert _extract_layer_ids({}) == set()

    def test_missing_sections_yield_empty_set(self):
        assert _extract_layer_ids({"layerConfig": {}}) == set()


def _json_handler(config=None, services=None, config_headers=None, services_headers=None):
    """An async MockTransport handler serving config.json at one URL and
    services.json at another, keyed by whether "config" or "services" is
    in the request path."""

    config = _SAMPLE_CONFIG_JSON if config is None else config
    services = _SAMPLE_SERVICES_JSON if services is None else services

    async def handler(request: httpx.Request) -> httpx.Response:
        if "config" in str(request.url):
            return httpx.Response(200, json=config, headers=config_headers or {})
        return httpx.Response(200, json=services, headers=services_headers or {})

    return handler


def _resolver(handler, **settings_kwargs) -> PortalCatalogResolver:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PortalCatalogResolver(GeodataSettings(**settings_kwargs), http_client=http_client)


_CONFIG_URL = "https://portal.example/config.json"
_SERVICES_URL = "https://portal.example/services.json"


class TestGetOrBuild:
    def test_missing_url_means_unscoped(self):
        resolver = _resolver(_json_handler())

        assert asyncio.run(resolver.get_or_build(None, _SERVICES_URL)) is None
        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, None)) is None
        assert asyncio.run(resolver.get_or_build("", "")) is None

    def test_fetches_and_cross_references_both(self):
        resolver = _resolver(_json_handler())

        catalog = asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL))

        assert catalog is not None
        assert catalog.has("27764")
        assert catalog.name("27764") == "StadtRAD-Stationen Hamburg"
        assert catalog.names() == {
            "453": "Digitale Orthophotos",
            "717": "Geobasiskarten Teil 1",
            "718": "Geobasiskarten Teil 2",
            "719": "Geobasiskarten Teil 3",
            "27764": "StadtRAD-Stationen Hamburg",
            "3302": "WMS-Gruppe A",
            "4561": "WMS-Gruppe B",
        }

    def test_layer_metadata_available_for_filter_agent_targeting(self):
        resolver = _resolver(_json_handler())

        catalog = asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL))

        match = catalog.get("27764")
        assert match is not None
        assert match.typ == "OAF"
        assert match.url == "https://api.hamburg.de/datasets/v1/stadtrad"
        assert match.collection == "stadtrad_stationen"

    def test_id_declared_in_config_but_missing_from_services_is_dropped(self):
        resolver = _resolver(_json_handler(services=[{"id": "27764", "typ": "OAF", "name": "StadtRAD"}]))

        catalog = asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL))

        assert catalog is not None
        assert not catalog.has("453")
        assert catalog.has("27764")

    def test_caches_result_within_ttl(self):
        calls = {"count": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if "config" in str(request.url):
                return httpx.Response(200, json=_SAMPLE_CONFIG_JSON)
            return httpx.Response(200, json=_SAMPLE_SERVICES_JSON)

        resolver = _resolver(handler, portal_catalog_cache_ttl=600.0)

        async def run():
            await resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)
            await resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)

        asyncio.run(run())

        assert calls["count"] == 2  # one fetch each for config.json + services.json

    def test_refetches_after_ttl_expires(self):
        calls = {"count": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if "config" in str(request.url):
                return httpx.Response(200, json=_SAMPLE_CONFIG_JSON)
            return httpx.Response(200, json=_SAMPLE_SERVICES_JSON)

        resolver = _resolver(handler, portal_catalog_cache_ttl=0.0)

        async def run():
            await resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)
            await resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)

        asyncio.run(run())

        assert calls["count"] == 4

    def test_caches_separately_per_url_pair(self):
        calls = {"count": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if "config" in str(request.url):
                return httpx.Response(200, json=_SAMPLE_CONFIG_JSON)
            return httpx.Response(200, json=_SAMPLE_SERVICES_JSON)

        resolver = _resolver(handler)

        async def run():
            await resolver.get_or_build("https://portal-a.example/config.json", "https://portal-a.example/services.json")
            await resolver.get_or_build("https://portal-b.example/config.json", "https://portal-b.example/services.json")

        asyncio.run(run())

        assert calls["count"] == 4

    def test_http_error_falls_back_to_none(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        resolver = _resolver(handler)

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is None

    def test_malformed_json_falls_back_to_none(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        resolver = _resolver(handler)

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is None

    def test_services_json_not_a_list_falls_back_to_none(self):
        resolver = _resolver(_json_handler(services={"not": "a list"}))

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is None

    def test_network_error_falls_back_to_none(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated network failure", request=request)

        resolver = _resolver(handler)

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is None


class TestConditionalGet:
    def test_304_reuses_previously_cached_body(self):
        state = {"calls": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            state["calls"] += 1
            if request.headers.get("if-none-match") == "\"v1\"":
                return httpx.Response(304)
            if "config" in str(request.url):
                return httpx.Response(200, json=_SAMPLE_CONFIG_JSON, headers={"etag": "\"v1\""})
            return httpx.Response(200, json=_SAMPLE_SERVICES_JSON, headers={"etag": "\"v1\""})

        resolver = _resolver(handler, portal_catalog_cache_ttl=0.0)

        async def run():
            first = await resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)
            second = await resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)
            return first, second

        first, second = asyncio.run(run())

        # 2 real fetches, then 2 conditional requests both answered 304
        assert state["calls"] == 4
        assert first is not None and second is not None
        assert first.names() == second.names()


class TestSingleFlight:
    def test_concurrent_cold_starts_build_once(self):
        calls = {"count": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            await asyncio.sleep(0.01)  # widen the race window
            if "config" in str(request.url):
                return httpx.Response(200, json=_SAMPLE_CONFIG_JSON)
            return httpx.Response(200, json=_SAMPLE_SERVICES_JSON)

        resolver = _resolver(handler)

        async def run():
            results = await asyncio.gather(
                resolver.get_or_build(_CONFIG_URL, _SERVICES_URL),
                resolver.get_or_build(_CONFIG_URL, _SERVICES_URL),
                resolver.get_or_build(_CONFIG_URL, _SERVICES_URL),
            )
            return results

        results = asyncio.run(run())

        assert all(r is not None for r in results)
        assert calls["count"] == 2  # one fetch each for config.json + services.json, not 6


class TestSsrfGuard:
    """A rejected URL must never actually be fetched - the handler
    raises if called, so any of these tests would fail loudly (not just
    return None for an unrelated reason) if the guard let the request
    through."""

    def _guarded_resolver(self, **settings_kwargs) -> PortalCatalogResolver:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"request should have been rejected before fetching: {request.url}")

        return _resolver(handler, **settings_kwargs)

    def test_rejects_cloud_metadata_ip_literal(self):
        resolver = self._guarded_resolver()

        result = asyncio.run(
            resolver.get_or_build("http://169.254.169.254/config.json", "http://169.254.169.254/services.json")
        )
        assert result is None

    def test_rejects_private_ip_literal(self):
        resolver = self._guarded_resolver()

        result = asyncio.run(resolver.get_or_build("http://10.0.0.5/config.json", "http://10.0.0.5/services.json"))
        assert result is None

    def test_loopback_allowed_by_default(self):
        """This project's only real deployment today runs frontend and
        backend on the same host - the frontend's own resolved URLs
        legitimately are loopback in that case, so rejecting them by
        default would silently disable scoping for the one setup that
        actually exists (confirmed live)."""

        resolver = _resolver(_json_handler())

        for host in ("127.0.0.1", "[::1]", "localhost"):
            result = asyncio.run(
                resolver.get_or_build(f"http://{host}/config.json", f"http://{host}/services.json")
            )
            assert result is not None

    def test_loopback_rejected_when_disabled(self):
        resolver = self._guarded_resolver(portal_catalog_allow_loopback=False)

        for host in ("127.0.0.1", "[::1]", "localhost"):
            result = asyncio.run(
                resolver.get_or_build(f"http://{host}/config.json", f"http://{host}/services.json")
            )
            assert result is None

    def test_rejects_non_http_scheme(self):
        resolver = self._guarded_resolver()

        result = asyncio.run(resolver.get_or_build("file:///etc/passwd", "file:///etc/shadow"))
        assert result is None

    def test_rejects_credentials_in_url(self):
        resolver = self._guarded_resolver()

        result = asyncio.run(
            resolver.get_or_build(
                "https://user:pass@portal.example/config.json", "https://user:pass@portal.example/services.json"
            )
        )
        assert result is None

    def test_rejects_second_url_even_if_first_is_fine(self):
        """Both URLs are validated - a disallowed services.json target
        must not be fetched just because config.json passed."""

        async def handler(request: httpx.Request) -> httpx.Response:
            if "10.0.0.5" in str(request.url):
                raise AssertionError("disallowed services.json URL should never be fetched")
            return httpx.Response(200, json=_SAMPLE_CONFIG_JSON)

        resolver = _resolver(handler)

        result = asyncio.run(resolver.get_or_build(_CONFIG_URL, "http://10.0.0.5/services.json"))
        assert result is None

    def test_allows_public_hostname_by_default(self):
        resolver = _resolver(_json_handler())

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is not None

    def test_allowlist_rejects_host_not_in_it(self):
        resolver = self._guarded_resolver(portal_catalog_allowed_hosts=frozenset({"other.example"}))

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is None

    def test_allowlist_permits_listed_host(self):
        resolver = _resolver(_json_handler(), portal_catalog_allowed_hosts=frozenset({"portal.example"}))

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is not None

    def test_rejects_response_over_content_length_cap(self):
        resolver = _resolver(
            _json_handler(config_headers={"content-length": "999999999"}), portal_catalog_max_response_bytes=1000
        )

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is None

    def test_rejects_response_over_actual_size_cap(self):
        oversized = {"layerConfig": {"subjectlayer": {"elements": [{"id": "x" * 5000}]}}}
        resolver = _resolver(_json_handler(config=oversized), portal_catalog_max_response_bytes=1000)

        assert asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL)) is None


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

    def _client(self):
        transport = httpx.MockTransport(_json_handler())
        client = httpx.AsyncClient(transport=transport)
        client.calls = []
        original_send = client.send

        async def recording_send(request, **kwargs):
            client.calls.append(str(request.url))
            return await original_send(request, **kwargs)

        client.send = recording_send
        return client

    def test_loopback_url_uses_the_loopback_client(self):
        strict, loopback = self._client(), self._client()
        resolver = PortalCatalogResolver(http_client=strict, loopback_http_client=loopback)

        asyncio.run(
            resolver.get_or_build("https://localhost:9001/config.json", "https://localhost:9001/services.json")
        )

        assert len(loopback.calls) == 2
        assert strict.calls == []

    def test_non_loopback_url_uses_the_strict_client(self):
        strict, loopback = self._client(), self._client()
        resolver = PortalCatalogResolver(http_client=strict, loopback_http_client=loopback)

        asyncio.run(resolver.get_or_build(_CONFIG_URL, _SERVICES_URL))

        assert len(strict.calls) == 2
        assert loopback.calls == []

    def test_loopback_disabled_never_calls_either_client(self):
        strict, loopback = self._client(), self._client()
        resolver = PortalCatalogResolver(
            GeodataSettings(portal_catalog_allow_loopback=False),
            http_client=strict,
            loopback_http_client=loopback,
        )

        result = asyncio.run(
            resolver.get_or_build("https://localhost:9001/config.json", "https://localhost:9001/services.json")
        )

        assert result is None
        assert strict.calls == []
        assert loopback.calls == []
