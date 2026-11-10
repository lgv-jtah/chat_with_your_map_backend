"""``LocationResolver``: free-text place -> ``LocationMatch``, for any city.

Combines the city's ``Geocoder`` (name -> point, kind and often only a
rough area geometry) with its ``BoundarySource`` (official outlines): for
an area hit, the geocoder's geometry is replaced by the official outline
when the source has one, so spatial filters cut at the real border.
"""

import logging

from cwym_agent.geodata.interfaces import BoundarySource, Geocoder
from cwym_agent.geodata.types import LocationMatch, bbox_of

logger = logging.getLogger(__name__)


class LocationResolver:
    def __init__(self, geocoder: Geocoder, boundaries: BoundarySource):
        self.geocoder = geocoder
        self.boundaries = boundaries

    def resolve(self, query: str) -> LocationMatch | None:
        """The best match for ``query``, or None."""

        match = self.geocoder.search(query)
        if match is None or match.kind is None:
            return match

        outline = self.boundaries.outline(match.name, match.kind)
        if outline is not None:
            match.boundary = outline
            match.extent = bbox_of(outline)
        return match
