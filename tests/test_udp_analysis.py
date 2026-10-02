"""Unit tests for the UDP agent client (cmym-agent-architecture.md Section 13)."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.udp_analysis import UdpAnalysisClient, format_history_and_query, parse_response

_FC = {
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [9.93, 53.55]}, "properties": {}}],
}


def _layer(**overrides):
    return {
        "label": "StadtRAD-Stationen in Altona",
        "role": "result",
        "geometry_type": "Point",
        "feature_count": 1,
        "total_features": 1,
        "truncated": False,
        "bbox": [9.93, 53.55, 9.93, 53.55],
        "features": _FC,
        **overrides,
    }


class TestParseResponse:
    def test_answer_and_layers(self):
        result = parse_response({"result": {"answer": "19 Stationen.", "map_layers": {"stations": _layer()}}})

        assert result.answer == "19 Stationen."
        assert result.error is None
        [layer] = result.layers
        assert layer.key == "stations"
        assert layer.role == "result"
        assert layer.feature_count == 1
        assert layer.bbox == (9.93, 53.55, 9.93, 53.55)

    def test_plain_string_result_means_no_layers(self):
        """A UDP deployment without map-layer support still answers."""

        result = parse_response({"result": "19 Stationen."})

        assert result.answer == "19 Stationen."
        assert result.layers == []

    def test_malformed_and_empty_layers_dropped(self):
        result = parse_response(
            {
                "result": {
                    "answer": "a",
                    "map_layers": {
                        "ok": _layer(),
                        "not_a_dict": "x",
                        "no_fc": _layer(features={"type": "Feature"}),
                        "empty": _layer(features={"type": "FeatureCollection", "features": []}),
                    },
                }
            }
        )

        assert [layer.key for layer in result.layers] == ["ok"]

    def test_unknown_role_falls_back_to_result(self):
        [layer] = parse_response({"result": {"answer": "a", "map_layers": {"k": _layer(role="highlight")}}}).layers

        assert layer.role == "result"

    def test_bbox_derived_from_features_not_the_udp_field(self):
        [layer] = parse_response({"result": {"answer": "a", "map_layers": {"k": _layer(bbox=[1, 2])}}}).layers

        assert layer.bbox == (9.93, 53.55, 9.93, 53.55)

    def test_out_of_extent_geometry_ignored_for_bbox(self):
        """Hamburg's StadtRAD data has a station at [0, 0]; its 200m
        buffer must not stretch the zoom extent to Null Island."""

        def ring(cx, cy, d=0.002):
            return [[[cx - d, cy - d], [cx + d, cy - d], [cx + d, cy + d], [cx - d, cy - d]]]

        union = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "MultiPolygon", "coordinates": [ring(0.0, 0.0), ring(9.99, 53.56)]},
                    "properties": {},
                }
            ],
        }
        [layer] = parse_response(
            {"result": {"answer": "a", "map_layers": {"k": _layer(features=union, bbox=[-0.002, -0.002, 9.992, 53.562])}}}
        ).layers

        assert layer.bbox == pytest.approx((9.988, 53.558, 9.992, 53.562))
        assert len(layer.features["features"][0]["geometry"]["coordinates"]) == 2

    def test_no_in_extent_geometry_means_no_bbox(self):
        null_island = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.0, 0.0]}, "properties": {}}],
        }
        [layer] = parse_response({"result": {"answer": "a", "map_layers": {"k": _layer(features=null_island)}}}).layers

        assert layer.bbox is None

    def test_unexpected_shape_is_an_error(self):
        result = parse_response({"something": "else"})

        assert result.answer is None
        assert result.error


class TestFormatHistoryAndQuery:
    def test_format_matches_udp_history_parser(self):
        text = format_history_and_query("StadtRAD", [("user", "Wie viele?"), ("assistant", "Welche meinen Sie?")])

        assert text.startswith("Chat History: [")
        assert text.endswith(" Current Question: StadtRAD")
        history = json.loads(text[len("Chat History: ") : text.index(" Current Question:")])
        assert history == [
            {"role": "user", "content": "Wie viele?"},
            {"role": "assistant", "content": "Welche meinen Sie?"},
        ]

    def test_empty_history(self):
        assert format_history_and_query("q", []) == "Chat History: [] Current Question: q"


class TestUdpAnalysisClient:
    def _client(self, handler, **settings):
        return UdpAnalysisClient(
            GeodataSettings(udp_agent_url="http://udp:1416/", **settings),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_posts_query_with_map_layers_opt_in(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["form"] = parse_qs(request.content.decode())
            return httpx.Response(200, json={"result": {"answer": "a", "map_layers": {}}})

        result = self._client(handler).analyze("Wie viele?", [("user", "x"), ("assistant", "y")])

        assert result.answer == "a"
        assert seen["url"] == "http://udp:1416/udp_agent/run"
        assert seen["form"]["include_map_layers"] == ["true"]
        assert seen["form"]["query"][0].endswith("Current Question: Wie viele?")

    def test_history_trimmed_to_setting(self):
        seen = {}

        def handler(request):
            seen["query"] = parse_qs(request.content.decode())["query"][0]
            return httpx.Response(200, json={"result": "a"})

        history = [("user", f"m{i}") for i in range(10)]
        self._client(handler, udp_agent_history_messages=2).analyze("q", history)

        assert '"m8"' in seen["query"] and '"m9"' in seen["query"]
        assert '"m7"' not in seen["query"]

    def test_http_error_becomes_error_result(self):
        result = self._client(lambda r: httpx.Response(500)).analyze("q")

        assert result.answer is None
        assert result.error

    def test_timeout_becomes_error_result(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)

        result = self._client(handler).analyze("q")

        assert result.answer is None
        assert "timed out" in result.error


def _square(cx, cy, d=0.002, hole=False):
    ring = [[cx - d, cy - d], [cx + d, cy - d], [cx + d, cy + d], [cx - d, cy + d], [cx - d, cy - d]]
    rings = [ring]
    if hole:
        h = d / 2
        rings.append([[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h], [cx - h, cy - h]])
    return {"type": "Polygon", "coordinates": rings}


def _fc(*geometries):
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": g, "properties": {"i": i}} for i, g in enumerate(geometries)],
    }


def _point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def _parse(result_fc, context_fc):
    return parse_response(
        {
            "result": {
                "answer": "a",
                "map_layers": {
                    "buffers": _layer(role="context", features=context_fc),
                    "schools": _layer(role="result", features=result_fc),
                },
            }
        }
    ).layers


class TestContextSafetyNet:
    """A proximity analysis can publish a buffer around every reference
    feature as context; only the buffers with a result inside are kept."""

    def test_drops_context_polygons_without_a_result(self):
        layers = _parse(
            _fc(_point(9.95, 53.55)),
            _fc(_square(9.95, 53.55), _square(10.05, 53.60), _square(10.10, 53.50)),
        )

        context = next(layer for layer in layers if layer.role == "context")
        assert [f["properties"]["i"] for f in context.features["features"]] == [0]
        assert context.feature_count == 1
        assert context.bbox == pytest.approx((9.948, 53.548, 9.952, 53.552))

    def test_multipolygon_kept_if_any_part_contains_a_result(self):
        multi = {"type": "MultiPolygon", "coordinates": [_square(10.10, 53.50)["coordinates"], _square(9.95, 53.55)["coordinates"]]}
        layers = _parse(_fc(_point(9.95, 53.55)), _fc(multi, _square(10.05, 53.60)))

        context = next(layer for layer in layers if layer.role == "context")
        assert context.feature_count == 1

    def test_result_in_a_hole_does_not_count(self):
        layers = _parse(_fc(_point(9.95, 53.55)), _fc(_square(9.95, 53.55, hole=True), _square(9.95, 53.551)))

        context = next(layer for layer in layers if layer.role == "context")
        assert [f["properties"]["i"] for f in context.features["features"]] == [1]

    def test_layer_untouched_if_no_polygon_contains_a_result(self):
        """E.g. a district boundary with the results just outside it."""

        context_fc = _fc(_square(10.05, 53.60), _square(10.10, 53.50))
        layers = _parse(_fc(_point(9.95, 53.55)), context_fc)

        context = next(layer for layer in layers if layer.role == "context")
        assert context.feature_count == 2

    def test_point_context_left_alone(self):
        layers = _parse(_fc(_point(9.95, 53.55)), _fc(_point(10.05, 53.60), _point(10.10, 53.50)))

        context = next(layer for layer in layers if layer.role == "context")
        assert context.feature_count == 2

    def test_no_result_layer_means_no_filtering(self):
        [context] = parse_response(
            {"result": {"answer": "a", "map_layers": {"b": _layer(role="context", features=_fc(_square(10.05, 53.60)))}}}
        ).layers

        assert context.feature_count == 1
