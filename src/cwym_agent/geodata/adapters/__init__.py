"""Data source adapters, and the factories that build them from a city
profile (ADR-002, cmym-agent-architecture.md Section 14).

Each profile section names its adapter (``geocoder.adapter`` etc.);
adding a city with different services means adding an adapter module
here plus its name to the ``Literal`` in ``cwym_agent.city_profile``.
Endpoint URLs come from the profile, but an environment variable
(``CWYM_ADDRESS_SEARCH_URL``, ``CWYM_AREA_BOUNDARIES_URL``,
``CWYM_LAYER_SEARCH_URL``) still overrides them, e.g. to point a test
environment at a mirror.
"""

import httpx

from cwym_agent.city_profile import BoundariesConfig, GeocoderConfig, LayerCatalogConfig
from cwym_agent.geodata.adapters.hamburg_addr_search import HamburgAddrSearchGeocoder
from cwym_agent.geodata.adapters.hamburg_layers_es import HamburgLayersEsSource
from cwym_agent.geodata.adapters.ogc_api_features import OgcApiFeaturesBoundaries
from cwym_agent.geodata.interfaces import BoundarySource, Geocoder, LayerCatalogSource
from cwym_agent.geodata.settings import GeodataSettings


def build_geocoder(config: GeocoderConfig, settings: GeodataSettings, http_client: httpx.Client) -> Geocoder:
    match config.adapter:
        case "hamburg_addr_search":
            return HamburgAddrSearchGeocoder(
                url=settings.address_search_url or config.url,
                broad_types=config.broad_types,
                place_types=config.place_types,
                http_client=http_client,
                result_size=settings.address_result_size,
            )
    raise ValueError(f"Unknown geocoder adapter {config.adapter!r}")


def build_boundary_source(
    config: BoundariesConfig, settings: GeodataSettings, http_client: httpx.Client
) -> BoundarySource:
    match config.adapter:
        case "ogc_api_features":
            return OgcApiFeaturesBoundaries(
                url=settings.area_boundaries_url or config.url,
                collections={kind: (c.collection, c.name_field) for kind, c in config.collections.items()},
                http_client=http_client,
                strip_name_prefixes=config.strip_name_prefixes,
            )
    raise ValueError(f"Unknown boundaries adapter {config.adapter!r}")


def build_layer_catalog_source(
    config: LayerCatalogConfig, settings: GeodataSettings, http_client: httpx.Client
) -> LayerCatalogSource:
    match config.adapter:
        case "hamburg_layers_es":
            return HamburgLayersEsSource(
                url=settings.layer_search_url or config.url,
                http_client=http_client,
                fetch_size=settings.layer_fetch_size,
                retries=settings.layer_fetch_retries,
                retry_backoff=settings.layer_fetch_retry_backoff,
            )
    raise ValueError(f"Unknown layer catalog adapter {config.adapter!r}")
