"""Geocoder adapter for Hamburg's address search (profile adapter
``hamburg_addr_search``).

Resolves a free-text location string (district, street, address) to
coordinates via Hamburg's public, unauthenticated Elasticsearch
address-search endpoint (``https://api.hamburg.de/addr_search``, a
search template).

This is a much lighter alternative to the ``HamburgAddressResolver``
Haystack component found in the sister `udp-chatbot` project: that
component returns a full ``geopandas.GeoDataFrame`` (via
``shapely``/``pyproj``) for downstream geoprocessing. CWYM only needs a
map center plus, for district/county-level hits, an area geometry to
draw/spatially restrict by (see ``cwym-agent-architecture.md`` Section
9) - both come directly off the same raw JSON response as plain WGS84
GeoJSON, no geometry library needed. The area geometry is only a
rectangle; ``LocationResolver`` replaces it with the official outline.

Which raw ``properties.type`` values count as areas (``broad_types``)
and how they map to CWYM location kinds (``place_types``) comes from the
city profile. Verified live (ImplementationLog.md): "county" is a Bezirk
(the 7 boroughs, e.g. "Altona", "Hamburg-Harburg"), "district" a
Stadtteil (e.g. "Altona-Altstadt", "Ottensen" - distinct from same-named
Bezirke, e.g. "Eimsbüttel" has both). "state"/"city" have no observed
hits and no clean equivalent, so they stay unmapped (kind=None).
"""

from collections.abc import Collection, Mapping

import httpx

from cwym_agent.geodata.adapters.es_addr_search import EsAddrSearchGeocoder


class HamburgAddrSearchGeocoder(EsAddrSearchGeocoder):
    """``Geocoder`` backed by Hamburg's address search (template
    ``addr_search``, parameter ``query_str``)."""

    def __init__(
        self,
        url: str,
        broad_types: Collection[str],
        place_types: Mapping[str, str],
        http_client: httpx.Client,
        result_size: int = 20,
        template_id: str = "addr_search",
        query_param: str = "query_str",
    ):
        super().__init__(
            url, broad_types, place_types, http_client, result_size, template_id, query_param
        )
