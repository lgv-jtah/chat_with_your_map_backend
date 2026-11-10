"""Unit tests for ``HamburgLayersEsSource`` (the layer catalog fetch and
parsing, built from the Hamburg profile) against a mocked HTTP transport.
"""

import httpx
import pytest

from cwym_agent.city_profile import load_profile
from cwym_agent.geodata.adapters import build_layer_catalog_source
from cwym_agent.geodata.settings import GeodataSettings

_PROFILE = load_profile("hamburg")


def _source(http_client: httpx.Client, settings: GeodataSettings | None = None):
    return build_layer_catalog_source(_PROFILE.layer_catalog, settings or GeodataSettings(), http_client)


def test_fetches_from_the_profile_url():
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url.copy_with(query=None)))
        return httpx.Response(200, json={"hits": {"hits": []}})

    _source(httpx.Client(transport=httpx.MockTransport(handler))).fetch_documents()

    assert urls == [_PROFILE.layer_catalog.url]


def _es_response(hits: list[dict]) -> dict:
    return {"hits": {"hits": [{"_source": source} for source in hits]}}


def _fetch(hits: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_es_response(hits))

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return _source(http_client).fetch_documents()


class TestFetchDocuments:
    def test_converts_hit_to_document(self):
        hits = [
            {
                "id": "27764",
                "name": "StadtRAD-Stationen Hamburg",
                "typ": "OAF",
                "url": "https://api.hamburg.de/datasets/v1/stadtrad",
                "datasets": [
                    {
                        "md_name": "StadtRAD-Stationen Hamburg",
                        "description": "Position aller StadtRAD-Stationen.\nWeitere Details folgen.",
                        "keywords": ["StadtRAD", "Fahrrad", "Station"],
                    }
                ],
            }
        ]

        documents = _fetch(hits)

        assert len(documents) == 1
        doc = documents[0]
        assert doc.meta["layer_id"] == "27764"
        assert doc.meta["layer_name"] == "StadtRAD-Stationen Hamburg"
        assert doc.meta["typ"] == "OAF"
        assert doc.meta["short_description"] == "Position aller StadtRAD-Stationen."
        assert "StadtRAD-Stationen Hamburg" in doc.content
        assert "Schlagwörter: StadtRAD, Fahrrad, Station" in doc.content

    def test_skips_hits_missing_id_or_name(self):
        hits = [
            {"id": "1", "name": None},
            {"id": None, "name": "Some Layer"},
            {"id": "2", "name": "Valid Layer"},
        ]

        documents = _fetch(hits)

        assert len(documents) == 1
        assert documents[0].meta["layer_id"] == "2"

    def test_handles_missing_datasets_gracefully(self):
        hits = [{"id": "3", "name": "No Dataset Info"}]

        documents = _fetch(hits)

        assert len(documents) == 1
        assert documents[0].meta["short_description"] == ""

    def test_empty_response_yields_no_documents(self):
        assert _fetch([]) == []


class TestFetchDocumentsRetry:
    """The layer catalog endpoint is intermittently flaky in practice
    (observed ~40% of requests timing out while others succeed in under
    a second) - a single transient failure shouldn't crash app startup.
    """

    def test_retries_after_transient_failure_then_succeeds(self):
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise httpx.ReadTimeout("simulated flaky timeout", request=request)
            return httpx.Response(
                200,
                json=_es_response([{"id": "1", "name": "Valid Layer"}]),
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        settings = GeodataSettings(layer_fetch_retries=5, layer_fetch_retry_backoff=0.0)

        documents = _source(http_client, settings).fetch_documents()

        assert attempts["count"] == 3
        assert len(documents) == 1
        assert documents[0].meta["layer_id"] == "1"

    def test_raises_after_exhausting_retries(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated persistent timeout", request=request)

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        settings = GeodataSettings(layer_fetch_retries=3, layer_fetch_retry_backoff=0.0)

        with pytest.raises(httpx.ReadTimeout):
            _source(http_client, settings).fetch_documents()
