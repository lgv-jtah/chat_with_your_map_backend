"""City profile loading and validation (CHAT-3, architecture Section 14.3)."""

import logging
from pathlib import Path

import pytest
import yaml

from cwym_agent import city_profile
from cwym_agent.city_profile import CityProfile, CityProfileError, available_profiles, load_profile
from cwym_agent.pipeline import build_pipeline
from cwym_agent.stubs.data_services import StubDataServices
from cwym_agent.stubs.qwen3 import StubQwen3Generator

REPO_PROFILES = Path(__file__).resolve().parents[1] / "profiles"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CWYM_CITY_PROFILE", raising=False)
    monkeypatch.delenv("CWYM_PROFILES_DIR", raising=False)


def _hamburg_data() -> dict:
    return yaml.safe_load((REPO_PROFILES / "hamburg" / "profile.yaml").read_text(encoding="utf-8"))


def _write_profile(tmp_path: Path, data: dict, name: str = "testcity") -> Path:
    data = {**data, "id": data.get("id", name)}
    path = tmp_path / name / "profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _load_variant(tmp_path, monkeypatch, mutate) -> CityProfile:
    data = _hamburg_data()
    data["id"] = "testcity"
    mutate(data)
    _write_profile(tmp_path, data)
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    return load_profile("testcity")


# --- selection -------------------------------------------------------------


def test_default_profile_is_hamburg():
    profile = load_profile()
    assert profile.id == "hamburg"
    assert profile.name == "Hamburg"
    assert profile.default_language == "de"
    assert profile.location_kinds == ["address", "bezirk", "stadtteil"]
    assert profile.analysis_agent is not None and profile.analysis_agent.adapter == "udp"


def test_repo_ships_hamburg_profile():
    assert "hamburg" in available_profiles(REPO_PROFILES)


def test_env_selects_profile_by_name(tmp_path, monkeypatch):
    _write_profile(tmp_path, {**_hamburg_data(), "id": "testcity", "name": "Teststadt"})
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("CWYM_CITY_PROFILE", "testcity")
    assert load_profile().name == "Teststadt"


def test_selector_can_be_a_file_path(tmp_path):
    path = _write_profile(tmp_path, {**_hamburg_data(), "id": "elsewhere"}, name="any-dir")
    assert load_profile(str(path)).id == "elsewhere"


def test_missing_profile_lists_available(tmp_path, monkeypatch):
    _write_profile(tmp_path, _hamburg_data(), name="hamburg")
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    with pytest.raises(CityProfileError) as e:
        load_profile("munich")
    msg = str(e.value)
    assert "'munich' not found" in msg and "Available" in msg and "hamburg" in msg
    assert "CWYM_CITY_PROFILE" in msg


def test_missing_profiles_dir_hints_at_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path / "nope"))
    with pytest.raises(CityProfileError, match="CWYM_PROFILES_DIR"):
        load_profile("hamburg")


def test_missing_profile_file_path(tmp_path):
    with pytest.raises(CityProfileError, match="file not found"):
        load_profile(str(tmp_path / "missing.yaml"))


# --- invalid files ---------------------------------------------------------


def test_invalid_yaml(tmp_path, monkeypatch):
    path = tmp_path / "broken" / "profile.yaml"
    path.parent.mkdir()
    path.write_text("id: broken\nname: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    with pytest.raises(CityProfileError, match="not valid YAML"):
        load_profile("broken")


def test_non_mapping_yaml(tmp_path, monkeypatch):
    path = tmp_path / "list" / "profile.yaml"
    path.parent.mkdir()
    path.write_text("- a\n- b\n", encoding="utf-8")
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    with pytest.raises(CityProfileError, match="must be a YAML mapping"):
        load_profile("list")


def test_id_must_match_directory(tmp_path, monkeypatch):
    _write_profile(tmp_path, {**_hamburg_data(), "id": "other"}, name="testcity")
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    with pytest.raises(CityProfileError, match="declares id 'other'"):
        load_profile("testcity")


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: d.update(nmae="typo"), "nmae: unknown field"),
        (lambda d: d["geocoder"].update(urll="x"), "geocoder.urll: unknown field"),
        (lambda d: d.pop("extent"), "extent: Field required"),
        (lambda d: d.update(languages=[]), "languages"),
        (lambda d: d.update(languages=["DE"]), "two lowercase letters"),
        (lambda d: d.update(languages=["de", "de"]), "unique"),
        (lambda d: d.update(languages=["de", "fr"]), "can't answer in ['fr']"),
        (lambda d: d.update(extent=[10.9, 52.8, 9.1, 54.1]), "min < max"),
        (lambda d: d["default_location"].update(lat=9.9937, lon=53.5511), "outside extent"),
        (lambda d: d["geocoder"].update(url="api.hamburg.de/addr_search"), "http(s) URL"),
        (lambda d: d["geocoder"].update(adapter="nominatim"), "geocoder.adapter"),
        (lambda d: d["boundaries"]["collections"].update(ortsteil={"collection": "x", "name_field": "y"}),
         "['ortsteil']"),
        (lambda d: d.update(analysis_agent={"adapter": "magic"}), "analysis_agent.adapter"),
        (lambda d: d["geocoder"]["place_types"].update(county="Bezirk"), "geocoder.place_types.county"),
        (lambda d: d.update(exclusive_layer_groups=[["a"]]), "at least 2 different layer ids"),
        (lambda d: d.update(exclusive_layer_groups=[["a", "b"], ["b", "c"]]), "more than one group"),
    ],
)
def test_invalid_values_name_the_field(tmp_path, monkeypatch, mutate, expected):
    with pytest.raises(CityProfileError) as e:
        _load_variant(tmp_path, monkeypatch, mutate)
    msg = str(e.value)
    assert msg.startswith("Invalid city profile") and "profile.yaml" in msg
    assert expected in msg


def test_analysis_agent_is_optional(tmp_path, monkeypatch):
    profile = _load_variant(tmp_path, monkeypatch, lambda d: d.pop("analysis_agent"))
    assert profile.analysis_agent is None


def test_profile_is_immutable():
    profile = load_profile()
    with pytest.raises(Exception):
        profile.name = "Altona"  # type: ignore[misc]


# --- pipeline wiring -------------------------------------------------------


def test_build_pipeline_loads_profile(caplog):
    with caplog.at_level(logging.INFO, logger="cwym_agent.pipeline"):
        pipeline = build_pipeline(llm=StubQwen3Generator(), data_services=StubDataServices())
    assert pipeline.metadata["city_profile"] == "hamburg"
    assert "City profile: hamburg (Hamburg)" in caplog.text


def test_build_pipeline_accepts_explicit_profile(tmp_path, monkeypatch):
    profile = _load_variant(tmp_path, monkeypatch, lambda d: d.update(name="Teststadt"))
    pipeline = build_pipeline(llm=StubQwen3Generator(), data_services=StubDataServices(), profile=profile)
    assert pipeline.metadata["city_profile"] == "testcity"


def test_build_pipeline_fails_before_warm_up(monkeypatch, tmp_path):
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("CWYM_CITY_PROFILE", "missing")

    class WarmUpSpy(StubDataServices):
        warmed = False

        def warm_up(self):
            WarmUpSpy.warmed = True

    with pytest.raises(CityProfileError):
        build_pipeline(llm=StubQwen3Generator(), data_services=WarmUpSpy())
    assert not WarmUpSpy.warmed


def test_startup_aborts_on_invalid_profile(monkeypatch, tmp_path, caplog):
    """The FastAPI lifespan logs a clear message and exits before warming up."""

    import asyncio

    from cwym_agent.api import main

    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("CWYM_CITY_PROFILE", "missing")
    main.get_pipeline.cache_clear()
    main.get_city_profile.cache_clear()
    monkeypatch.setattr(main, "get_pipeline", lambda: pytest.fail("warm-up must not start"))

    async def run_lifespan():
        async with main.lifespan(main.app):
            pass

    try:
        with caplog.at_level(logging.ERROR, logger="cwym_agent.api.main"):
            with pytest.raises(SystemExit) as e:
                asyncio.run(run_lifespan())
        assert e.value.code == 1
        assert "Startup aborted" in caplog.text and "'missing' not found" in caplog.text
    finally:
        monkeypatch.undo()
        main.get_pipeline.cache_clear()
        main.get_city_profile.cache_clear()


def test_profiles_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CWYM_PROFILES_DIR", str(tmp_path))
    assert city_profile.profiles_dir() == tmp_path


# --- tier 1 datasets (CHAT-11) ---------------------------------------------


class TestDatasets:
    def test_hamburg_declares_none(self):
        assert load_profile("hamburg").datasets == {}

    def test_dataset_loads_with_resolved_file(self, tmp_path):
        from fixtures.datasets import buildings_profile

        profile = buildings_profile(tmp_path)
        dataset = profile.datasets["buildings"]

        assert dataset.source.path == (tmp_path / "testcity" / "buildings.geojson").resolve()
        assert dataset.attributes["energieeffizienzklasse"].order[0] == "A+"
        assert profile.dataset_for_layer("test_gebaeude")[0] == "buildings"
        assert profile.dataset_for_layer("test_energieklasse")[0] == "buildings"
        assert profile.dataset_for_layer("27764") is None

    @pytest.mark.parametrize(
        "mutate, expected",
        [
            (lambda d: d["datasets"]["buildings"]["source"].update(path="nope.geojson"), "dataset file not found"),
            (lambda d: d["datasets"]["buildings"]["source"].update(type="shapefile"), "source"),
            (lambda d: d["datasets"]["buildings"]["attributes"]["energieeffizienzklasse"].pop("order"), "needs 'order'"),
            (lambda d: d["datasets"]["buildings"]["attributes"]["eigentuemer"].update(order=["a"]), "only for ordinal"),
            (lambda d: d["datasets"]["buildings"]["attributes"]["eigentuemer"].update(bin_width=10), "only for number"),
            (lambda d: d["datasets"]["buildings"]["attributes"]["baujahr"].update(top=3), "only for category"),
            (lambda d: d["datasets"]["buildings"]["attributes"]["energieeffizienzklasse"]["colors"].update(Z="#000000"), "not in 'order'"),
            (lambda d: d["datasets"]["buildings"]["attributes"]["energieeffizienzklasse"]["colors"].update(H="red"), "colors"),
            (lambda d: d["datasets"]["buildings"]["attributes"]["baujahr"].update(type="date"), "type"),
            (lambda d: d["datasets"]["buildings"]["attributes"]["baujahr"]["label"].pop("en"), "has no text for ['en']"),
            (lambda d: d["datasets"]["buildings"]["label"].pop("de"), "datasets.buildings.label has no text for ['de']"),
            (lambda d: d["datasets"].update(other={**d["datasets"]["buildings"], "layers": ["test_gebaeude"]}), "belongs to datasets"),
        ],
    )
    def test_invalid_datasets_name_the_field(self, tmp_path, mutate, expected):
        from fixtures.datasets import buildings_profile

        with pytest.raises(CityProfileError) as e:
            buildings_profile(tmp_path, mutate)
        assert expected in str(e.value)
