"""Interfaces between the city-independent core and a city's data sources.

ADR-002 (cmym-agent-architecture.md Section 14): one codebase, one
deployment per city. Everything that depends on a city's own services
sits behind these protocols; the city profile names the adapter for each
(``geocoder.adapter``, ``boundaries.adapter``, ``layer_catalog.adapter``)
and ``cwym_agent.geodata.adapters`` builds it.

``DataServices`` is the facade ``AgentExecutorComponent`` dispatches the
planned sub-agent calls to; ``RealDataServices`` composes it from the
sources, ``StubDataServices`` fakes it for tests.
"""

from collections.abc import Collection
from typing import Protocol

from haystack import Document

from cwym_agent.geodata.types import ActionMatch, AnalysisResult, DatasetAnswer, FilterResult, LayerMatch, LocationMatch


class Geocoder(Protocol):
    """A city's place search: free text -> one place."""

    def search(self, query: str) -> LocationMatch | None:
        """The best match for ``query``, or None if nothing matches or the
        service fails.

        The match carries ``kind`` (one of the profile's location kinds,
        or None for a hit that is none of them, e.g. a street),
        ``alternatives`` when the query names several areas equally well,
        and ``boundary``/``extent`` when the service returns an area
        geometry. ``LocationResolver`` replaces that geometry with the
        official outline from the ``BoundarySource`` where there is one.
        """
        ...


class BoundarySource(Protocol):
    """Official outlines of a city's administrative areas."""

    def outline(self, name: str, kind: str) -> dict | None:
        """The outline (GeoJSON Polygon/MultiPolygon, WGS84) of the area
        ``name`` of location kind ``kind``, or None if the source has no
        such area, doesn't cover that kind, or fails."""
        ...


class LayerCatalogSource(Protocol):
    """A city's map layer catalog, indexed once by ``LayerSearchService``."""

    def fetch_documents(self) -> list[Document]:
        """All layers as Haystack documents: ``content`` is the searchable
        text; ``meta`` holds ``layer_id``, ``layer_name``, ``typ``,
        ``url``, ``collection`` and ``short_description`` (one line for
        the LLM disambiguation prompt). Raises on failure - the catalog
        is fetched at startup, and an empty index is worse than a clear
        startup error."""
        ...


class DataServices(Protocol):
    """What ``AgentExecutorComponent`` calls for each planned sub-agent.

    Implementations may also have ``warm_up()``; ``build_pipeline`` calls
    it if present.
    """

    def resolve_locations(self, query: str) -> list[LocationMatch]: ...

    def resolve_layer(self, query: str, allowed_layer_ids: Collection[str] | None = None) -> LayerMatch | None: ...

    def detect_action(self, query: str) -> ActionMatch: ...

    def analyze(self, query: str, history: list[tuple[str, str]] | None = None) -> AnalysisResult: ...

    def apply_filter(
        self,
        query: str,
        layer: LayerMatch | None,
        location: LocationMatch | None = None,
        mode: str = "replace",
        prior_conditions: list | None = None,
    ) -> FilterResult | None: ...

    def analyze_dataset(
        self, query: str, operation: str, layer: LayerMatch | None, location: LocationMatch | None = None
    ) -> DatasetAnswer:
        """A tier 1 analysis (architecture 14.4): one fixed operation."""
        ...
