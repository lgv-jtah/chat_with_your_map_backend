"""Unit tests for ``RealDataServices``.

Uses a fake ``layer_search``/``address_resolver`` double (so retrieval
quality itself isn't exercised here - that's ``test_layer_search.py`` +
the opt-in live test) and a ``Qwen3Client`` mocked via
``httpx2.MockTransport`` (same approach as ``test_qwen3_client.py``) to
deterministically test the expansion -> merge -> dedupe ->
disambiguation flow that bridges colloquial terms to official layer
names.
"""

import json

import httpx2

from cwym_agent.geodata.real_data_services import _HAMBURG_EXTENT, RealDataServices
from cwym_agent.geodata.types import ActionMatch, LayerMatch, LocationMatch
from cwym_agent.llm.qwen3_client import Qwen3Client
from cwym_agent.settings import Qwen3Settings


class FakeLayerSearch:
    """Returns canned candidates per query, recording calls made."""

    def __init__(self, results_by_query: dict[str, list[LayerMatch]]):
        self.results_by_query = results_by_query
        self.queries: list[str] = []
        self.allowed_layer_ids_seen: list = []

    def search(self, query: str, allowed_layer_ids=None) -> list[LayerMatch]:
        self.queries.append(query)
        self.allowed_layer_ids_seen.append(allowed_layer_ids)
        return self.results_by_query.get(query, [])


class FakeAddressResolver:
    def __init__(self, result: LocationMatch | None):
        self.result = result
        self.queries: list[str] = []

    def resolve(self, query: str) -> LocationMatch | None:
        self.queries.append(query)
        return self.result


def _sse_text_response(text: str) -> str:
    chunk = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def _mock_llm(replies: list[str]) -> Qwen3Client:
    """A Qwen3Client that returns each of ``replies`` in order, one per call."""

    call_index = {"value": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        index = call_index["value"]
        call_index["value"] += 1
        text = replies[index] if index < len(replies) else replies[-1]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_text_response(text),
        )

    http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return Qwen3Client(Qwen3Settings(base_url="http://test.invalid/v1"), http_client=http_client)


class TestResolveLayer:
    def test_expands_query_and_disambiguates_with_llm(self):
        wrong_match = LayerMatch("Fahrradluftstationen", "1", 0.9)
        right_match = LayerMatch("StadtRAD-Stationen Hamburg", "2", 0.4, "Bike-Sharing")

        layer_search = FakeLayerSearch(
            {
                "Fahrradstationen": [wrong_match],
                "StadtRAD": [right_match],
            }
        )
        # First LLM call: query expansion -> "StadtRAD". Second call:
        # disambiguation among the merged [wrong, right] candidates -> "2".
        llm = _mock_llm(["StadtRAD", "2"])

        service = RealDataServices(layer_search=layer_search, llm=llm)
        result = service.resolve_layer("Fahrradstationen")

        assert result == right_match
        assert "Fahrradstationen" in layer_search.queries
        assert "StadtRAD" in layer_search.queries

    def test_forwards_allowed_layer_ids_to_layer_search(self):
        match = LayerMatch("StadtRAD-Stationen Hamburg", "27764", 0.9)
        layer_search = FakeLayerSearch({"Fahrradstationen": [match]})
        llm = _mock_llm([""])  # no expansion terms; single candidate, no disambiguation

        service = RealDataServices(layer_search=layer_search, llm=llm)
        service.resolve_layer("Fahrradstationen", allowed_layer_ids={"27764", "18105"})

        assert layer_search.allowed_layer_ids_seen == [{"27764", "18105"}]

    def test_no_allowed_layer_ids_means_unscoped(self):
        match = LayerMatch("StadtRAD-Stationen Hamburg", "27764", 0.9)
        layer_search = FakeLayerSearch({"Fahrradstationen": [match]})
        llm = _mock_llm([""])

        service = RealDataServices(layer_search=layer_search, llm=llm)
        service.resolve_layer("Fahrradstationen")

        assert layer_search.allowed_layer_ids_seen == [None]

    def test_no_candidates_returns_none(self):
        layer_search = FakeLayerSearch({})
        llm = _mock_llm([""])

        service = RealDataServices(layer_search=layer_search, llm=llm)

        assert service.resolve_layer("nichts passendes") is None

    def test_single_candidate_skips_llm_disambiguation(self):
        only_match = LayerMatch("Krankenhäuser", "5", 0.5)
        layer_search = FakeLayerSearch({"Krankenhäuser": [only_match]})
        # Expansion call returns no extra terms; no disambiguation call
        # should be needed/made since there's only one candidate.
        llm = _mock_llm([""])

        service = RealDataServices(layer_search=layer_search, llm=llm)
        result = service.resolve_layer("Krankenhäuser")

        assert result == only_match

    def test_llm_reply_of_zero_means_no_match(self):
        a = LayerMatch("A", "1", 0.5)
        b = LayerMatch("B", "2", 0.4)
        layer_search = FakeLayerSearch({"query": [a, b]})
        llm = _mock_llm(["", "0"])

        service = RealDataServices(layer_search=layer_search, llm=llm)
        result = service.resolve_layer("query")

        assert result is None

    def test_unparseable_llm_reply_falls_back_to_top_candidate(self):
        a = LayerMatch("A", "1", 0.5)
        b = LayerMatch("B", "2", 0.4)
        layer_search = FakeLayerSearch({"query": [a, b]})
        llm = _mock_llm(["", "not a number"])

        service = RealDataServices(layer_search=layer_search, llm=llm)
        result = service.resolve_layer("query")

        assert result == a

    def test_dedupes_candidates_by_name_keeping_highest_confidence(self):
        low = LayerMatch("StadtRAD-Stationen Hamburg", "1", 0.3)
        high = LayerMatch("StadtRAD-Stationen Hamburg", "2", 0.9)
        layer_search = FakeLayerSearch({"query": [low, high]})
        llm = _mock_llm([""])  # single candidate after dedup -> no disambiguation call

        service = RealDataServices(layer_search=layer_search, llm=llm)
        result = service.resolve_layer("query")

        assert result == high


class TestResolveLocations:
    def test_bare_hamburg_query_uses_default_without_calling_resolver(self):
        address_resolver = FakeAddressResolver(LocationMatch("Hamburg-Harburg", 1.0, 2.0, 0.9))
        service = RealDataServices(
            address_resolver=address_resolver, layer_search=FakeLayerSearch({})
        )

        result = service.resolve_locations("Hamburg")

        assert result == [LocationMatch("Hamburg", 53.5511, 9.9937, 0.9, extent=_HAMBURG_EXTENT)]
        assert address_resolver.queries == []

    def test_resolved_match_is_returned(self):
        match = LocationMatch("Wandsbek", 53.6, 10.1, 0.8)
        address_resolver = FakeAddressResolver(match)
        service = RealDataServices(
            address_resolver=address_resolver, layer_search=FakeLayerSearch({})
        )

        assert service.resolve_locations("Wandsbek") == [match]

    def test_unresolved_query_falls_back_to_default_hamburg_location(self):
        address_resolver = FakeAddressResolver(None)
        service = RealDataServices(
            address_resolver=address_resolver, layer_search=FakeLayerSearch({})
        )

        result = service.resolve_locations("kompletter nonsens")

        assert result == [LocationMatch("Hamburg", 53.5511, 9.9937, 0.9, extent=_HAMBURG_EXTENT)]


class TestDetectAction:
    def test_keyword_matches(self):
        service = RealDataServices(
            address_resolver=FakeAddressResolver(None), layer_search=FakeLayerSearch({})
        )

        assert service.detect_action("Navigiere nach Altona") == ActionMatch("Navigieren", 0.9)
        assert service.detect_action("Filtere die Ergebnisse") == ActionMatch("Filter", 0.9)
        assert service.detect_action("Öffne die Karte") == ActionMatch("Öffne", 0.9)

    def test_default_action(self):
        service = RealDataServices(
            address_resolver=FakeAddressResolver(None), layer_search=FakeLayerSearch({})
        )

        assert service.detect_action("Zeige mir Fahrradstationen") == ActionMatch("Zeigen", 0.85)
