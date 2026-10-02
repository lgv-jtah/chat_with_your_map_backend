"""Client for the UDP agent (cmym-agent-architecture.md Section 13).

The UDP agent is a separate, independently deployed Haystack pipeline
(``udp-chatbot`` repo, self-hosted Hayhooks) that answers analytical
questions about Hamburg's Urban Data Platform: counts, areas, nearest
features, comparisons. CWYM calls it as its slowest sub-agent, only for
questions its own show/filter/navigate tools can't answer.

The contract is the UDP agent's own public API, nothing CWYM-specific:
``POST /{pipeline}/run`` with ``include_map_layers=true`` returns
``{"result": {"answer": str, "map_layers": {key: layer}}}``, where each
layer is a WGS84 GeoJSON FeatureCollection the UDP Execution Agent chose
to publish (its ``publish_map_layer`` tool). Layers are untrusted input
here like any other remote response: malformed entries are dropped, not
passed on to the map.
"""

import dataclasses
import json
import logging

import httpx

from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import HAMBURG_EXTENT, AnalysisResult, ResultLayer

logger = logging.getLogger(__name__)

_ROLES = ("result", "context")


def format_history_and_query(query: str, history: list[tuple[str, str]]) -> str:
    """The single string the UDP pipeline's DeepsetChatHistoryParser
    expects - same format as the UDP repo's own evaluation client
    (``evaluation/clients/hayhooks.py``)."""

    messages = [{"role": role, "content": text} for role, text in history]
    return f"Chat History: {json.dumps(messages, ensure_ascii=False)} Current Question: {query}"


def _positions(coordinates):
    """Every [x, y, ...] position in a GeoJSON ``coordinates`` array,
    whatever its nesting depth (Point through MultiPolygon)."""

    if not isinstance(coordinates, list) or not coordinates:
        return
    if isinstance(coordinates[0], (int, float)):
        yield coordinates
        return
    for item in coordinates:
        yield from _positions(item)


def _bbox_within(feature_list: list, extent: tuple[float, float, float, float]):
    """The layer's bbox, computed from its own coordinates rather than
    trusting the UDP agent's ``bbox`` field - and only from positions
    inside ``extent``. Found live: Hamburg's StadtRAD dataset has a
    station at [0, 0] ("Bahnhof Dammtor"), so a buffer-around-stations
    analysis published a polygon at Null Island and the union bbox ran
    from the Gulf of Guinea to Hamburg - ZOOM_TO_EXTENT then left the
    map showing nothing useful. Out-of-extent geometry is still drawn,
    it just doesn't drive the zoom."""

    min_x, min_y, max_x, max_y = extent
    xs: list[float] = []
    ys: list[float] = []
    for feature in feature_list:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(geometry, dict):
            continue
        for position in _positions(geometry.get("coordinates")):
            try:
                x, y = float(position[0]), float(position[1])
            except (IndexError, TypeError, ValueError):
                continue
            if min_x <= x <= max_x and min_y <= y <= max_y:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _parse_layer(key: str, raw) -> ResultLayer | None:
    if not isinstance(raw, dict):
        return None
    features = raw.get("features")
    if not isinstance(features, dict) or features.get("type") != "FeatureCollection":
        return None
    feature_list = features.get("features")
    if not isinstance(feature_list, list) or not feature_list:
        return None

    bbox = _bbox_within(feature_list, HAMBURG_EXTENT)
    role = raw.get("role") if raw.get("role") in _ROLES else "result"
    return ResultLayer(
        key=str(key),
        label=str(raw.get("label") or key),
        role=role,
        features=features,
        feature_count=len(feature_list),
        total_features=int(raw.get("total_features") or len(feature_list)),
        truncated=bool(raw.get("truncated", False)),
        geometry_type=raw.get("geometry_type"),
        bbox=bbox,
    )


def _in_ring(x: float, y: float, ring: list) -> bool:
    """Ray casting point-in-ring test (boundary cases don't matter here)."""

    inside = False
    for (x1, y1, *_), (x2, y2, *_) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _polygons(geometry) -> list | None:
    """A Polygon/MultiPolygon's polygons (lists of rings), or None for
    any other geometry type."""

    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return None
    if geometry.get("type") == "Polygon":
        return [coordinates]
    if geometry.get("type") == "MultiPolygon":
        return coordinates
    return None


def _contains_any(polygons: list, points: list[tuple[float, float]]) -> bool:
    for polygon in polygons:
        try:
            outer, holes = polygon[0], polygon[1:]
            xs = [p[0] for p in outer]
            ys = [p[1] for p in outer]
        except (IndexError, TypeError):
            continue
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
        for x, y in points:
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                continue
            if _in_ring(x, y, outer) and not any(_in_ring(x, y, hole) for hole in holes):
                return True
    return False


def _filter_context_layers(layers: list[ResultLayer]) -> list[ResultLayer]:
    """Drop context-layer polygons that contain none of the result
    features - a safety net for proximity questions. Found live: despite
    the UDP prompt's rule to publish only matched reference features, a
    "Schulen im Umkreis von 200m zu einer Fahrradstation" run still
    published a buffer around all 358 stations as context, so the map
    showed every station instead of the 77 with a school nearby.

    Only Polygon/MultiPolygon features are filtered (a point context
    layer, e.g. all stations as points, has no area to test against);
    a layer is left untouched if filtering would remove every polygon,
    so context that legitimately contains no result (e.g. a district
    boundary next to results outside it) still shows."""

    points: list[tuple[float, float]] = []
    for layer in layers:
        if layer.role != "result":
            continue
        for feature in layer.features.get("features", []):
            geometry = feature.get("geometry") if isinstance(feature, dict) else None
            if isinstance(geometry, dict):
                for position in _positions(geometry.get("coordinates")):
                    try:
                        points.append((float(position[0]), float(position[1])))
                    except (IndexError, TypeError, ValueError):
                        continue
    if not points:
        return layers

    filtered = []
    for layer in layers:
        if layer.role != "context":
            filtered.append(layer)
            continue
        feature_list = layer.features["features"]
        kept, kept_polygons, dropped = [], 0, 0
        for feature in feature_list:
            polygons = _polygons(feature.get("geometry") if isinstance(feature, dict) else None)
            if polygons is None:
                kept.append(feature)
            elif _contains_any(polygons, points):
                kept.append(feature)
                kept_polygons += 1
            else:
                dropped += 1
        if dropped == 0 or kept_polygons == 0:
            filtered.append(layer)
            continue
        logger.info(
            "udp_analysis: context layer %r - dropped %d of %d polygons containing no result feature",
            layer.key,
            dropped,
            len(feature_list),
        )
        filtered.append(
            dataclasses.replace(
                layer,
                features={**layer.features, "features": kept},
                feature_count=len(kept),
                total_features=len(kept),
                bbox=_bbox_within(kept, HAMBURG_EXTENT),
            )
        )
    return filtered


def parse_response(data) -> AnalysisResult:
    """Hayhooks wraps ``run_api_async``'s return value under ``result``.
    A plain-string result means a UDP deployment without map-layer
    support - still a valid answer, just nothing to draw."""

    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, str):
        return AnalysisResult(answer=result)
    if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
        return AnalysisResult(answer=None, error="unexpected response shape from the UDP agent")

    layers = []
    raw_layers = result.get("map_layers") or {}
    if isinstance(raw_layers, dict):
        for key, raw in raw_layers.items():
            layer = _parse_layer(key, raw)
            if layer is None:
                logger.info("udp_analysis: dropping malformed or empty map layer %r", key)
                continue
            layers.append(layer)
    return AnalysisResult(answer=result["answer"], layers=_filter_context_layers(layers))


class UdpAnalysisClient:
    def __init__(self, settings: GeodataSettings | None = None, http_client: httpx.Client | None = None):
        self.settings = settings or GeodataSettings()
        self._client = http_client or httpx.Client(timeout=self.settings.udp_agent_timeout)
        self._endpoint = (
            f"{self.settings.udp_agent_url.rstrip('/')}/{self.settings.udp_agent_pipeline}/run"
        )

    def analyze(self, query: str, history: list[tuple[str, str]] | None = None) -> AnalysisResult:
        recent = (history or [])[-self.settings.udp_agent_history_messages :]
        try:
            # Multipart form, as current Hayhooks expects for /run.
            response = self._client.post(
                self._endpoint,
                data={"query": format_history_and_query(query, recent), "include_map_layers": "true"},
            )
            response.raise_for_status()
            result = parse_response(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("udp_analysis: UDP agent request failed (%s)", exc)
            return AnalysisResult(answer=None, error=str(exc) or type(exc).__name__)

        logger.info(
            "udp_analysis: answer %d chars, layers %r",
            len(result.answer or ""),
            [(layer.key, layer.role, layer.feature_count) for layer in result.layers],
        )
        return result
