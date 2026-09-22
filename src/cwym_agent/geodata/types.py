"""Shared result types for location/layer/action resolution.

Used by both ``cwym_agent.stubs.data_services.StubDataServices`` and
``cwym_agent.geodata.real_data_services.RealDataServices``, so both can
be swapped in behind the same ``AgentExecutorComponent`` without it
needing to know which implementation is in use.
"""

from dataclasses import dataclass


@dataclass
class LocationMatch:
    name: str
    latitude: float
    longitude: float
    confidence: float
    boundary: dict | None = None
    """GeoJSON Polygon/MultiPolygon (WGS84), or None when the resolved hit
    has no area geometry (street/address-level matches, or the source
    endpoint's area field was itself a Point rather than a polygon)."""


@dataclass
class LayerMatch:
    name: str
    layer_id: str
    confidence: float
    description: str = ""


@dataclass
class ActionMatch:
    action_type: str
    confidence: float
