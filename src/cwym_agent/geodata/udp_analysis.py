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

import json
import logging

import httpx

from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import AnalysisResult, ResultLayer

logger = logging.getLogger(__name__)

_ROLES = ("result", "context")


def format_history_and_query(query: str, history: list[tuple[str, str]]) -> str:
    """The single string the UDP pipeline's DeepsetChatHistoryParser
    expects - same format as the UDP repo's own evaluation client
    (``evaluation/clients/hayhooks.py``)."""

    messages = [{"role": role, "content": text} for role, text in history]
    return f"Chat History: {json.dumps(messages, ensure_ascii=False)} Current Question: {query}"


def _parse_layer(key: str, raw) -> ResultLayer | None:
    if not isinstance(raw, dict):
        return None
    features = raw.get("features")
    if not isinstance(features, dict) or features.get("type") != "FeatureCollection":
        return None
    feature_list = features.get("features")
    if not isinstance(feature_list, list) or not feature_list:
        return None

    bbox = raw.get("bbox")
    try:
        bbox = tuple(float(v) for v in bbox) if bbox is not None else None
    except (TypeError, ValueError):
        bbox = None
    if bbox is not None and len(bbox) != 4:
        bbox = None

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
    return AnalysisResult(answer=result["answer"], layers=layers)


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
