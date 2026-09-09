"""``ResultAggregatorComponent``: combines sub-agent results.

Combines the results from all sub-agent calls (location, layer, action)
into a single unified result set, and publishes the resulting map state
(center location, zoom, layer visibility) as an AG-UI ``STATE_SNAPSHOT``
event, so the frontend can patch Masterportal's Vuex store directly and
have the map update live as the query resolves.
"""

from haystack import component

from cwym_agent.bridge import AGUIEventBridge

# Default zoom level applied when a single location is resolved. Actual
# zoom-to-extent behavior for multiple locations is left to the frontend.
_DEFAULT_ZOOM = 9


@component
class ResultAggregatorComponent:
    @component.output_types(aggregated=dict)
    def run(self, execution_log: list, query: str, bridge: AGUIEventBridge | None = None):
        by_agent = {entry["agent_name"]: entry["result"] for entry in execution_log}

        locations = by_agent.get("location_extractor", [])
        layer = by_agent.get("layer_resolver")
        action = by_agent.get("action_detector")

        aggregated = {
            "query": query,
            "locations": locations,
            "layer": layer,
            "action": action,
        }

        if bridge:
            bridge.state_snapshot(self._build_map_snapshot(locations, layer, action))

        return {"aggregated": aggregated}

    def _build_map_snapshot(self, locations, layer, action) -> dict:
        snapshot: dict = {"action": action.action_type if action else None}

        if locations:
            primary = locations[0]
            snapshot["center"] = {
                "latitude": primary.latitude,
                "longitude": primary.longitude,
            }
            snapshot["zoom"] = _DEFAULT_ZOOM

        if layer:
            snapshot["layers"] = [
                {
                    "id": layer.layer_id,
                    "name": layer.name,
                    "visible": True,
                }
            ]

        return snapshot
