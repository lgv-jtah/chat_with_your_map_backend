"""The Munich city profile against recorded responses of its public
endpoints (tests/fixtures/munich/README.md), served by a mock transport."""

import json
from pathlib import Path

import httpx
import pytest

from cwym_agent.city_profile import available_profiles, load_profile
from cwym_agent.components.agent_executor import AgentExecutorComponent
from cwym_agent.geodata.adapters import build_boundary_source, build_geocoder, build_layer_catalog_source
from cwym_agent.geodata.filter_agent import FilterAgent
from cwym_agent.geodata.locations import LocationResolver
from cwym_agent.geodata.real_data_services import RealDataServices
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import LayerMatch, LocationMatch
from cwym_agent.language import resolve_language

FIXTURES = Path(__file__).parent / "fixtures" / "munich"
PROFILE = load_profile("munich")


def _json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


_ADDRESS_FIXTURES = {
    "Hugo-Wolf-Straße 57": "addr_hugo_wolf_57.json",
    "Milbertshofen-Am Hart": "addr_milbertshofen.json",
    "Hugo-Wolf-Straße": "addr_hugo_wolf_street.json",
    "Harthof": "addr_harthof.json",
}
_WFS_FIXTURES = {
    "gsm_wfs:vablock_stadtbezirk": "wfs_stadtbezirk.json",
    "gsm_wfs:ascend_umgriff_projektgebiet": "wfs_projektgebiet.json",
}


class _Server:
    def __init__(self, fail_wfs: set[str] = frozenset()):
        self.address_sources: list[dict] = []
        self.address_urls: list[str] = []
        self.wfs_requests = 0
        self.fail_wfs = fail_wfs

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/search/p1_adressen_4326/" in url:
            source = json.loads(request.url.params["source"])
            self.address_sources.append(source)
            self.address_urls.append(str(request.url.copy_with(query=None)))
            fixture = _ADDRESS_FIXTURES.get(source["params"]["search_term"])
            if fixture is None:
                return httpx.Response(200, json={"hits": {"hits": []}})
            return httpx.Response(200, json=_json(fixture))
        if "/gsm_wfs/wfs" in url:
            self.wfs_requests += 1
            type_name = request.url.params["typeNames"]
            if type_name in self.fail_wfs:
                return httpx.Response(503)
            return httpx.Response(200, json=_json(_WFS_FIXTURES[type_name]))
        return httpx.Response(404)


def _client(server: _Server) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server.handler))


def _geocoder(server: _Server):
    return build_geocoder(PROFILE.geocoder, GeodataSettings(), _client(server))


def _resolver(server: _Server) -> LocationResolver:
    client = _client(server)
    return LocationResolver(
        build_geocoder(PROFILE.geocoder, GeodataSettings(), client),
        build_boundary_source(PROFILE.boundaries, GeodataSettings(), client),
        PROFILE.named_areas,
    )


def _vertices(boundary: dict) -> int:
    polygons = [boundary["coordinates"]] if boundary["type"] == "Polygon" else boundary["coordinates"]
    return sum(len(ring) for polygon in polygons for ring in polygon)


def _inside(extent, bbox) -> bool:
    return extent[0] <= bbox[0] and extent[1] <= bbox[1] and bbox[2] <= extent[2] and bbox[3] <= extent[3]


def test_profile_loads():
    assert PROFILE.languages == ["de", "en"]
    assert PROFILE.location_kinds == ["address", "projektgebiet", "stadtbezirk"]
    assert "munich" in available_profiles()


class TestGeocoder:
    def test_request_uses_the_profile_template(self):
        server = _Server()
        _geocoder(server).search("Harthof")

        assert server.address_sources[0]["id"] == "all_features"
        assert server.address_sources[0]["params"]["search_term"] == "Harthof"
        assert server.address_urls == [PROFILE.geocoder.url]

    def test_address_has_no_boundary_and_single_spaced_name(self):
        match = _geocoder(_Server()).search("Hugo-Wolf-Straße 57")

        assert match.name == "Hugo-Wolf-Straße 57"
        assert match.kind == "address"
        assert match.boundary is None
        assert match.extent is None

    def test_stadtbezirk_keeps_the_rectangle_without_crs(self):
        match = _geocoder(_Server()).search("Milbertshofen-Am Hart")

        assert match.kind == "stadtbezirk"
        assert match.boundary["type"] == "Polygon"
        assert "crs" not in match.boundary
        assert match.extent is not None

    def test_unknown_name_is_none(self):
        assert _geocoder(_Server()).search("Harthof") is None

    def test_street_is_unmapped(self):
        match = _geocoder(_Server()).search("Hugo-Wolf-Straße")

        assert match.kind is None
        assert match.boundary is None

    def test_http_error_is_none(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        assert build_geocoder(PROFILE.geocoder, GeodataSettings(), client).search("Harthof") is None


class TestLocationResolver:
    def test_stadtbezirk_gets_the_wfs_outline(self):
        match = _resolver(_Server()).resolve("Milbertshofen-Am Hart")

        assert match.kind == "stadtbezirk"
        assert _vertices(match.boundary) > 5
        assert _inside(PROFILE.extent, match.extent)

    def test_split_stadtbezirk_is_a_multipolygon(self):
        server = _Server()
        resolver = _resolver(server)
        features = _json("wfs_stadtbezirk.json")["features"]
        assert sum(f["properties"]["sb_name"] == "Untergiesing-Harlaching" for f in features) == 2

        outline = resolver.boundaries.outline("Untergiesing-Harlaching", "stadtbezirk")

        assert outline["type"] == "MultiPolygon"
        assert len(outline["coordinates"]) == 2

    @pytest.mark.parametrize("query", ["Harthof", "harthof project area", "Projektgebiet Harthof"])
    def test_named_area(self, query: str):
        from shapely.geometry import Point, shape

        server = _Server()
        match = _resolver(server).resolve(query)

        assert match.name == "Harthof"
        assert match.kind == "projektgebiet"
        assert match.confidence == 1.0
        assert match.boundary["type"] in ("Polygon", "MultiPolygon")
        assert shape(match.boundary).contains(Point(match.longitude, match.latitude))
        assert server.address_sources == []

    def test_failing_named_area_falls_back_to_the_city(self):
        server = _Server(fail_wfs={"gsm_wfs:ascend_umgriff_projektgebiet"})
        client = _client(server)
        services = RealDataServices(
            PROFILE,
            locations=LocationResolver(
                build_geocoder(PROFILE.geocoder, GeodataSettings(), client),
                build_boundary_source(PROFILE.boundaries, GeodataSettings(), client),
                PROFILE.named_areas,
            ),
            layer_search=object(),
            filter_agent=object(),
            llm=object(),
            udp_analysis=object(),
        )

        locations = services.resolve_locations("Harthof")

        assert [server.address_sources[0]["params"]["search_term"]] == ["Harthof"]
        assert locations == [services.city_location]


class TestLayerIndex:
    def _documents(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_json("layers_all.json"))))
        return build_layer_catalog_source(PROFILE.layer_catalog, GeodataSettings(), client).fetch_documents()

    def test_all_layers_become_documents(self):
        assert len(self._documents()) == 35

    def test_bilingual_name_is_searchable(self):
        document = next(d for d in self._documents() if d.meta["layer_id"] == "gsm:ascend_baugruppen_energieeffizienzklasse")

        assert "building energy efficiency class" in document.content
        assert document.meta["layer_name"] == "ASCEND Energieeffizienzklasse | building energy efficiency class"
        assert document.meta["typ"] is None

    def test_layer_without_url_or_datasets_is_kept(self):
        document = next(d for d in self._documents() if d.meta["layer_id"] == "terrain")

        assert document.meta["url"] is None


_WMS_LAYER = LayerMatch(
    name="ASCEND Energieeffizienzklasse",
    layer_id="gsm:ascend_baugruppen_energieeffizienzklasse",
    confidence=0.9,
    typ="WMS",
    url="https://geoportal.muenchen.de/geoserver/gsm/wms",
)


def _harthof() -> LocationMatch:
    return _resolver(_Server()).resolve("Harthof")


class _NoLlm:
    def generate(self, prompt: str, max_tokens=None, label="generate") -> str:
        return "[]"


class TestFilters:
    def test_wms_layer_is_not_filtered(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP expected")

        agent = FilterAgent(
            PROFILE.prompt.area_terms, llm=_NoLlm(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

        assert agent.filter("Energieeffizienzklasse in Harthof", _WMS_LAYER, _harthof()) is None

    def test_no_implicit_filter_for_wms_layer(self):
        results = {"layer_resolver": _WMS_LAYER, "location_extractor": [_harthof()]}

        assert AgentExecutorComponent._implicit_filter_query([], results, None) is None

    def test_wfs_filter_against_geoserver(self):
        posts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, text=(FIXTURES / "wfs_bezirksteil_describe.xml").read_text())
            posts.append(request.content.decode())
            if 'resultType="hits"' in posts[-1]:
                return httpx.Response(200, text='<wfs:FeatureCollection numberMatched="2"/>')
            return httpx.Response(200, json=sample)

        harthof = _harthof()
        # FilterAgent re-checks features against the area, so the sample
        # gets one feature inside Harthof next to the recorded ones.
        inside = {
            "type": "Feature",
            "properties": {"bt_nummer": "99"},
            "geometry": {"type": "Point", "coordinates": [harthof.longitude, harthof.latitude]},
        }
        recorded = _json("wfs_bezirksteil_sample.json")
        sample = {**recorded, "features": [*recorded["features"], inside]}

        agent = FilterAgent(
            PROFILE.prompt.area_terms, llm=_NoLlm(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        layer = LayerMatch(
            name="Bezirksteile",
            layer_id="x",
            confidence=0.9,
            typ="WFS",
            url="https://geoportal.muenchen.de/geoserver/gsm_wfs/wfs",
            feature_type="vablock_bezirksteil",
        )

        result = agent.filter("Bezirksteile in Harthof", layer, harthof)

        body = posts[-1]
        assert 'typeNames="app:vablock_bezirksteil"' in body
        assert 'xmlns:app="gsm_wfs"' in body
        assert "<fes:Intersects><fes:ValueReference>shape</fes:ValueReference><gml:" in body
        assert result.features["features"] == [inside]


@pytest.mark.parametrize(
    ("query", "language"),
    [
        ("Show me the building energy efficiency class in Harthof", "en"),
        ("Zeige die Energieeffizienzklasse in Harthof", "de"),
    ],
)
def test_language(query: str, language: str):
    assert resolve_language(query, [], None, PROFILE.languages).code == language
