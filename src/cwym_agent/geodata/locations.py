"""``LocationResolver``: free-text place -> ``LocationMatch``, for any city.

Combines the city's ``Geocoder`` (name -> point, kind and often only a
rough area geometry) with its ``BoundarySource`` (official outlines): for
an area hit, the geocoder's geometry is replaced by the official outline
when the source has one, so spatial filters cut at the real border.

Areas the profile declares (``named_areas``, e.g. a project area) are
answered from their boundary collection before the geocoder is asked:
the profile wins over the geocoder.
"""

import logging
from collections.abc import Sequence

from shapely.geometry import shape

from cwym_agent.city_profile import NamedArea
from cwym_agent.geodata.portal_catalog_resolver import name_key
from cwym_agent.geodata.interfaces import BoundarySource, Geocoder
from cwym_agent.geodata.types import LocationMatch, bbox_of

logger = logging.getLogger(__name__)


class LocationResolver:
    def __init__(self, geocoder: Geocoder, boundaries: BoundarySource, named_areas: Sequence[NamedArea] = ()):
        self.geocoder = geocoder
        self.boundaries = boundaries
        self._named = {name_key(n): area for area in named_areas for n in (area.name, *area.aliases)}

    def resolve(self, query: str) -> LocationMatch | None:
        """The best match for ``query``, or None."""

        named = self._named.get(name_key(query))
        if named is not None:
            match = self._named_match(named)
            if match is not None:
                return match

        match = self.geocoder.search(query)
        if match is None or match.kind is None:
            return match

        outline = self.boundaries.outline(match.name, match.kind)
        if outline is not None:
            match.boundary = outline
            match.extent = bbox_of(outline)
        return match

    def _named_match(self, area: NamedArea) -> LocationMatch | None:
        outline = self.boundaries.collection_outline(area.collection)
        if outline is None:
            logger.warning("No outline for named area %r (collection %r)", area.name, area.collection)
            return None
        point = shape(outline).representative_point()
        return LocationMatch(
            name=area.name,
            latitude=point.y,
            longitude=point.x,
            confidence=1.0,
            boundary=outline,
            extent=bbox_of(outline),
            kind=area.kind,
        )
