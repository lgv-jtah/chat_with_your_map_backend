"""A test profile with a tier 1 dataset: the Hamburg profile plus the
fictional ``buildings`` dataset in ``buildings.geojson`` (12 buildings,
values stored as strings like ASCEND's cache, with missing values, an
ordinal "A+", decimal commas, duplicate and compound addresses and a
polygon with an inner ring)."""

import shutil
from pathlib import Path

import yaml

from cwym_agent.city_profile import CityProfile, load_profile

FIXTURES = Path(__file__).resolve().parent
REPO_PROFILES = FIXTURES.parents[1] / "profiles"

BUILDINGS = {
    "label": {"de": "Gebäude", "en": "buildings"},
    "source": {"type": "file", "path": "buildings.geojson"},
    "id_field": "gebid",
    "lookup_field": "adresse",
    "layers": ["test_gebaeude"],
    "attributes": {
        "baujahr": {
            "label": {"de": "Baujahr", "en": "year of construction"},
            "type": "number",
            "bin_width": 10,
            "display_layer": "test_baujahr",
        },
        "geschosse": {"label": {"de": "Geschosse", "en": "floors"}, "type": "number"},
        "pv_leistung": {"label": {"de": "PV-Leistung (kWp)", "en": "PV output (kWp)"}, "type": "number"},
        "eigentuemer": {
            "label": {"de": "Eigentümer", "en": "owner"},
            "type": "category",
            "top": 2,
            "display_layer": "test_eigentuemer",
        },
        "energieeffizienzklasse": {
            "label": {"de": "Energieeffizienzklasse", "en": "energy efficiency class"},
            "type": "ordinal",
            "order": ["A+", "A", "B", "C", "D", "E", "F", "G", "H"],
            "colors": {"A+": "#16a34a", "H": "#991b1b"},
            "display_layer": "test_energieklasse",
        },
    },
}


def hamburg_data() -> dict:
    return yaml.safe_load((REPO_PROFILES / "hamburg" / "profile.yaml").read_text(encoding="utf-8"))


def buildings_profile(tmp_path: Path, mutate=None) -> CityProfile:
    """The Hamburg profile with the ``buildings`` dataset, loaded from a
    temporary profile directory (so the dataset file resolves)."""

    data = hamburg_data()
    data["id"] = "testcity"
    data["datasets"] = {"buildings": yaml.safe_load(yaml.safe_dump(BUILDINGS))}
    if mutate is not None:
        mutate(data)
    directory = tmp_path / "testcity"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    shutil.copy(FIXTURES / "buildings.geojson", directory / "buildings.geojson")
    return load_profile(str(directory / "profile.yaml"))
