"""Opt-in live tests of the Munich profile (set ``CWYM_LIVE_TESTS=1``):
its public endpoints, and layer resolution with the real embedder/LLM."""

import os

import httpx
import pytest

from cwym_agent.city_profile import load_profile
from cwym_agent.geodata.analysis_data import WfsSource
from cwym_agent.geodata.adapters import build_boundary_source, build_geocoder, build_layer_catalog_source
from cwym_agent.geodata.filter_agent import FilterAgent
from cwym_agent.geodata.locations import LocationResolver
from cwym_agent.geodata.real_data_services import RealDataServices
from cwym_agent.geodata.wfs_filter import WfsClient
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import FilterCondition, LayerMatch

pytestmark = pytest.mark.skipif(
    os.environ.get("CWYM_LIVE_TESTS") != "1",
    reason="Set CWYM_LIVE_TESTS=1 to run tests against the real geodata services.",
)

PROFILE = load_profile("munich")


@pytest.fixture(scope="module")
def resolver() -> LocationResolver:
    client = httpx.Client(timeout=60)
    settings = GeodataSettings()
    return LocationResolver(
        build_geocoder(PROFILE.geocoder, settings, client),
        build_boundary_source(PROFILE.boundaries, settings, client),
        PROFILE.named_areas,
    )


def test_address(resolver: LocationResolver):
    match = resolver.resolve("Hugo-Wolf-Straße 57")

    assert match is not None and match.kind == "address" and match.boundary is None


def test_stadtbezirk_outline(resolver: LocationResolver):
    match = resolver.resolve("Milbertshofen-Am Hart")

    assert match is not None and match.kind == "stadtbezirk"
    assert len(match.boundary["coordinates"][0]) > 5


def test_project_area(resolver: LocationResolver):
    match = resolver.resolve("Harthof")

    assert match is not None and match.kind == "projektgebiet" and match.boundary


def test_layer_catalog_size():
    client = httpx.Client(timeout=60)
    documents = build_layer_catalog_source(PROFILE.layer_catalog, GeodataSettings(), client).fetch_documents()

    assert len(documents) >= 30


def test_wfs_spatial_filter(resolver: LocationResolver):
    harthof = resolver.resolve("Harthof")
    layer = LayerMatch(
        name="Bezirksteile",
        layer_id="x",
        confidence=1.0,
        typ="WFS",
        url="https://geoportal.muenchen.de/geoserver/gsm_wfs/wfs",
        feature_type="vablock_bezirksteil",
    )

    class _NoLlm:
        def generate(self, prompt: str, max_tokens=None, label="generate") -> str:
            return "[]"

    result = FilterAgent(PROFILE.prompt.area_terms, llm=_NoLlm()).filter("Bezirksteile in Harthof", layer, harthof)

    assert result is not None and result.number_matched is not None
    assert 0 < result.number_matched < 110


@pytest.fixture(scope="module")
def real_data_services() -> RealDataServices:
    service = RealDataServices(PROFILE)
    service.warm_up()
    return service


@pytest.mark.parametrize("query", ["Energieeffizienzklasse", "building energy efficiency class"])
def test_energy_layer_in_both_languages(real_data_services: RealDataServices, query: str):
    result = real_data_services.resolve_layer(query)

    assert result is not None and "Energieeffizienz" in result.name


class TestBuildingsLive:
    """The real WFS behind the ``buildings`` dataset (CHAT-12), no LLM."""

    @pytest.fixture
    def source(self) -> WfsSource:
        config = PROFILE.datasets["buildings"]
        settings = GeodataSettings()
        wfs = WfsClient(settings, httpx.Client(timeout=60))
        schema = wfs.schema(config.source.url, config.source.feature_type)
        assert schema is not None
        return WfsSource(config.source.url, config.source.feature_type, schema, config.attributes, wfs, settings)

    def test_whole_dataset(self, source: WfsSource):
        assert source.fetch([], None, False).total >= 235

    def test_energy_class_h(self, source: WfsSource):
        found = source.fetch([FilterCondition("energieeffizienzklasse", "eq", "H")], None, True)

        assert found.complete and 100 <= found.total <= 273  # 130 when written

    def test_umlaut_attribute_in_the_post_body(self, source: WfsSource):
        found = source.fetch([FilterCondition("gebäudezustand", "eq", "Abgerissen")], None, True)

        assert found.complete and found.total > 0
