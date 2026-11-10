"""City profiles: everything that makes a CWYM deployment city-specific.

ADR-002 (cmym-agent-architecture.md Section 14): one codebase, one
deployment per city. The city is chosen at startup with
``CWYM_CITY_PROFILE`` - either a profile name, resolved to
``<profiles dir>/<name>/profile.yaml``, or a path to a profile YAML file.
The profiles directory defaults to ``profiles/`` at the repository root
and can be moved with ``CWYM_PROFILES_DIR`` (the Docker image sets it).

The profile is validated strictly when it's loaded, so a typo or a missing
value stops startup with a message naming the file and the field, instead
of surfacing as a wrong answer at query time.

The data sources are built from the profile by
``cwym_agent.geodata.adapters`` (each section names its adapter), and the
prompts and tool descriptions are worded with its ``prompt`` section.
Nothing city-specific lives outside the profiles and the city's adapters
(``tests/test_no_city_in_core.py``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from cwym_agent.language import SUPPORTED_LANGUAGES

DEFAULT_PROFILE = "hamburg"
PROFILE_ENV = "CWYM_CITY_PROFILE"
PROFILES_DIR_ENV = "CWYM_PROFILES_DIR"
PROFILE_FILE = "profile.yaml"

# Repository root /profiles - right for a source checkout (uv run, tests).
# The Docker image installs the package non-editable, so it sets
# CWYM_PROFILES_DIR instead.
_REPO_PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


class CityProfileError(RuntimeError):
    """A city profile is missing or invalid. The message is meant for the
    operator reading the startup log."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _http_url(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"must be an http(s) URL, got {value!r}")
    return value


HttpUrlStr = Annotated[str, AfterValidator(_http_url)]

LocationKind = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)]
"""A CWYM location kind: a city's own name for a kind of place (e.g.
stadtteil, address). The profile defines them (values of
``geocoder.place_types``); they travel to the frontend in map commands
and come back in the client state (``lastLocation``, ``filterSpec.area``),
and the planner sees them in its context block."""


class DefaultLocation(_Strict):
    """Where 'the city' itself points to (the address search usually has no
    single city-wide hit)."""

    name: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class GeocoderConfig(_Strict):
    adapter: Literal["hamburg_addr_search"]
    url: HttpUrlStr
    # Raw place types of the service that count as areas (preferred over
    # street hits for a bare place name).
    broad_types: list[str] = Field(min_length=1)
    # Raw place type -> CWYM location kind (e.g. county -> bezirk).
    place_types: dict[str, LocationKind] = Field(min_length=1)


class LayerCatalogConfig(_Strict):
    adapter: Literal["hamburg_layers_es"]
    url: HttpUrlStr


class BoundaryCollection(_Strict):
    collection: str = Field(min_length=1)
    name_field: str = Field(min_length=1)


class BoundariesConfig(_Strict):
    adapter: Literal["ogc_api_features"]
    url: HttpUrlStr
    # Prefixes the geocoder puts on some area names that the boundary
    # dataset doesn't have (e.g. "<City>-North" -> "North").
    strip_name_prefixes: list[str] = Field(default_factory=list)
    # CWYM location kind -> collection holding its official outlines.
    collections: dict[LocationKind, BoundaryCollection] = Field(min_length=1)


class AnalysisAgentConfig(_Strict):
    """Tier 2 analysis agent (Section 14.4). Endpoint and timeouts stay in
    environment variables (CWYM_UDP_AGENT_*), they differ per environment."""

    adapter: Literal["udp"]
    example_question: str = Field(min_length=1)
    """An analytical question about this city's data, shown to the planner
    in the tool description."""


# --- tier 1 datasets (cmym-agent-architecture.md 14.4) ----------------------

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)]
HexColor = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
LocalizedText = dict[str, Annotated[str, Field(min_length=1)]]
"""Language code -> text; must cover every profile language."""


class FileSourceConfig(_Strict):
    """A GeoJSON FeatureCollection (WGS84) loaded at startup."""

    type: Literal["file"]
    path: Path
    """Relative to the profile's directory."""

    @field_validator("path")
    @classmethod
    def _resolve(cls, v: Path, info: ValidationInfo) -> Path:
        base_dir = (info.context or {}).get("base_dir")
        if base_dir is None:
            return v
        resolved = (Path(base_dir) / v).resolve()
        if not resolved.is_file():
            raise ValueError(f"dataset file not found: {resolved}")
        return resolved


class WfsSourceConfig(_Strict):
    type: Literal["wfs"]
    url: HttpUrlStr
    feature_type: str = Field(min_length=1)
    """Without namespace prefix, e.g. "staatliche_schulen"."""


class OafSourceConfig(_Strict):
    type: Literal["oaf"]
    url: HttpUrlStr
    """The API's landing page (``.../collections`` is appended)."""
    collection: str = Field(min_length=1)


DatasetSourceConfig = Annotated[
    FileSourceConfig | WfsSourceConfig | OafSourceConfig, Field(discriminator="type")
]


class AttributeConfig(_Strict):
    """One attribute tier 1 may compute on, with its type (14.4 rule 2:
    values are converted by type before sorting or computing)."""

    label: LocalizedText
    type: Literal["number", "category", "ordinal"]
    order: list[str] | None = None
    """ordinal only: all values, lowest/best first (e.g. A+, A, B, ..., H)."""
    bin_width: float | None = Field(default=None, gt=0)
    """number only: distribution in bins of this width (10 = decades)."""
    top: int | None = Field(default=None, ge=1)
    """category only: distribution shows the ``top`` most frequent values
    plus "Sonstige"."""
    colors: dict[str, HexColor] = Field(default_factory=dict)
    """Value -> chart colour."""
    display_layer: str | None = None
    """Portal layer that colours the dataset by this attribute; switched on
    for a distribution of it."""

    @model_validator(mode="after")
    def _type_options(self) -> AttributeConfig:
        if self.type == "ordinal":
            if not self.order:
                raise ValueError("an ordinal attribute needs 'order' (all values, lowest/best first)")
            if len(set(self.order)) != len(self.order):
                raise ValueError("'order' values must be unique")
            unknown = sorted(set(self.colors) - set(self.order))
            if unknown:
                raise ValueError(f"colors for values not in 'order': {unknown}")
        elif self.order is not None:
            raise ValueError("'order' is only for ordinal attributes")
        if self.bin_width is not None and self.type != "number":
            raise ValueError("'bin_width' is only for number attributes")
        if self.top is not None and self.type != "category":
            raise ValueError("'top' is only for category attributes")
        if self.colors and self.type == "number" and self.bin_width is None:
            raise ValueError("'colors' on a number attribute need 'bin_width' (they colour bins)")
        return self


class DatasetConfig(_Strict):
    """A tier 1 dataset: one source, typed attributes."""

    label: LocalizedText
    """Plural noun for replies, e.g. {de: Gebäude, en: buildings}."""
    source: DatasetSourceConfig
    id_field: str = Field(min_length=1)
    lookup_field: str | None = None
    """Property for lookups by name or address (e.g. "adresse")."""
    layers: list[str] = Field(default_factory=list)
    """Portal layer ids that show this dataset - how a resolved layer finds
    its dataset (the attributes' display layers count too)."""
    attributes: dict[str, AttributeConfig] = Field(min_length=1)

    @property
    def layer_ids(self) -> set[str]:
        return set(self.layers) | {a.display_layer for a in self.attributes.values() if a.display_layer}


class LayerExample(_Strict):
    """A colloquial term whose official layer has a different name - the
    example that teaches the planner and the layer search's query
    expansion what to do."""

    term: str = Field(min_length=1)
    """What a user says, e.g. "Fahrradstationen"."""
    layer_name: str = Field(min_length=1)
    """The official layer name, e.g. "StadtRAD-Stationen"."""
    official_term: str = Field(min_length=1)
    """The official term to search for, e.g. "StadtRAD"."""
    official_term_reason: str = Field(min_length=1)
    """Completes "weil ... offiziell so heißt", e.g. "das städtische
    Bike-Sharing-System"."""


class PromptConfig(_Strict):
    """City-specific wording in the prompts and tool descriptions (German)."""

    city_genitive: str = Field(min_length=1)
    """e.g. "Münchens" ("Münchens Geodaten-Katalog")."""
    area_terms: list[str] = Field(min_length=1)
    """Words for the city's areas, most common first, e.g. ["Stadtteil",
    "Bezirk"]."""
    example_places: list[str] = Field(min_length=2)
    """Place names for examples, e.g. ["Wandsbek", "Altona"]."""
    neighbouring_regions: list[str] = Field(min_length=1)
    """Regions whose same-named layers the layer selection must not
    prefer, e.g. ["Niedersachsen", "Schleswig-Holstein"]."""
    layer_example: LayerExample


class CityProfile(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    """Short city name used in prompts and replies, e.g. "München"."""
    official_name: str = Field(min_length=1)
    languages: list[str] = Field(min_length=1)
    """Supported answer languages, ISO 639-1; the first is the default."""
    default_location: DefaultLocation
    extent: tuple[float, float, float, float]
    """WGS84 (minLon, minLat, maxLon, maxLat): the 'whole city' map view."""
    geocoder: GeocoderConfig
    layer_catalog: LayerCatalogConfig
    boundaries: BoundariesConfig
    prompt: PromptConfig
    analysis_agent: AnalysisAgentConfig | None = None
    """None: the city has no tier 2 analysis agent."""
    datasets: dict[Identifier, DatasetConfig] = Field(default_factory=dict)
    """Tier 1 datasets with typed attributes (14.4). Counting works on any
    filterable layer without one; the other operations need it."""

    @field_validator("languages")
    @classmethod
    def _languages(cls, v: list[str]) -> list[str]:
        for code in v:
            if len(code) != 2 or not code.isalpha() or not code.islower():
                raise ValueError(f"language codes must be two lowercase letters (ISO 639-1), got {code!r}")
        if len(set(v)) != len(v):
            raise ValueError("language codes must be unique")
        unsupported = [code for code in v if code not in SUPPORTED_LANGUAGES]
        if unsupported:
            raise ValueError(
                f"CWYM can't answer in {unsupported} yet (supported: {list(SUPPORTED_LANGUAGES)})"
            )
        return v

    @field_validator("extent")
    @classmethod
    def _extent(cls, v: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        min_lon, min_lat, max_lon, max_lat = v
        if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
            raise ValueError(
                "must be WGS84 [minLon, minLat, maxLon, maxLat] with min < max "
                f"(lon -180..180, lat -90..90), got {list(v)}"
            )
        return v

    @model_validator(mode="after")
    def _consistent(self) -> CityProfile:
        min_lon, min_lat, max_lon, max_lat = self.extent
        loc = self.default_location
        if not (min_lon <= loc.lon <= max_lon and min_lat <= loc.lat <= max_lat):
            raise ValueError(
                f"default_location ({loc.lat}, {loc.lon}) lies outside extent {list(self.extent)} "
                "- lat/lon swapped?"
            )
        kinds = set(self.geocoder.place_types.values())
        unknown = sorted(set(self.boundaries.collections) - kinds)
        if unknown:
            raise ValueError(
                f"boundaries.collections has kinds {unknown} that no geocoder.place_types entry produces "
                f"(known kinds: {sorted(kinds)})"
            )
        for key, dataset in self.datasets.items():
            texts = {f"datasets.{key}.label": dataset.label} | {
                f"datasets.{key}.attributes.{name}.label": attribute.label
                for name, attribute in dataset.attributes.items()
            }
            for where, text in texts.items():
                missing = [code for code in self.languages if code not in text]
                if missing:
                    raise ValueError(f"{where} has no text for {missing} (profile languages: {self.languages})")
        layer_owner: dict[str, str] = {}
        for key, dataset in self.datasets.items():
            for layer_id in dataset.layer_ids:
                if layer_owner.setdefault(layer_id, key) != key:
                    raise ValueError(f"layer {layer_id!r} belongs to datasets {layer_owner[layer_id]!r} and {key!r}")
        return self

    def dataset_for_layer(self, layer_id: str) -> tuple[str, DatasetConfig] | None:
        """The declared dataset a portal layer shows, if any."""

        return next(((key, d) for key, d in self.datasets.items() if layer_id in d.layer_ids), None)

    @property
    def default_language(self) -> str:
        return self.languages[0]

    @property
    def location_kinds(self) -> list[str]:
        """CWYM location kinds this city knows (e.g. bezirk, stadtteil, address)."""
        return sorted(set(self.geocoder.place_types.values()))


def profiles_dir() -> Path:
    raw = os.environ.get(PROFILES_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else _REPO_PROFILES_DIR


def available_profiles(directory: Path | None = None) -> list[str]:
    directory = directory or profiles_dir()
    if not directory.is_dir():
        return []
    return sorted(p.parent.name for p in directory.glob(f"*/{PROFILE_FILE}"))


def _resolve(selector: str) -> tuple[Path, str | None]:
    """The profile file for a name or path, and the name it must declare as
    ``id`` (None when selected by path)."""

    if selector.endswith((".yaml", ".yml")) or os.sep in selector:
        path = Path(selector).expanduser()
        if not path.is_file():
            raise CityProfileError(f"City profile file not found: {path}")
        return path, None

    directory = profiles_dir()
    path = directory / selector / PROFILE_FILE
    if not path.is_file():
        available = available_profiles(directory)
        raise CityProfileError(
            f"City profile {selector!r} not found: expected {path}. "
            f"Available in {directory}: {', '.join(available) or 'none'}. "
            f"Set {PROFILE_ENV} to one of them or to the path of a profile YAML file"
            + (f", or point {PROFILES_DIR_ENV} at the profiles directory." if not available else ".")
        )
    return path, selector


def _format_errors(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "(top level)"
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            msg = "unknown field (typo? not supported yet?)"
        lines.append(f"  - {where}: {msg}")
    return "\n".join(lines)


def load_profile(selector: str | None = None) -> CityProfile:
    """Load and validate a city profile.

    :param selector: profile name or path; defaults to ``CWYM_CITY_PROFILE``,
        then ``hamburg``.
    :raises CityProfileError: the profile is missing or invalid.
    """

    selector = (selector or os.environ.get(PROFILE_ENV, "") or DEFAULT_PROFILE).strip()
    path, expected_id = _resolve(selector)

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise CityProfileError(f"City profile {path} is not valid YAML: {e}") from e
    except OSError as e:
        raise CityProfileError(f"City profile {path} cannot be read: {e}") from e
    if not isinstance(data, dict):
        raise CityProfileError(f"City profile {path} must be a YAML mapping, got {type(data).__name__}")

    try:
        profile = CityProfile.model_validate(data, context={"base_dir": path.parent})
    except ValidationError as e:
        raise CityProfileError(f"Invalid city profile {path}:\n{_format_errors(e)}") from None

    if expected_id is not None and profile.id != expected_id:
        raise CityProfileError(
            f"City profile {path} declares id {profile.id!r} but is stored as {expected_id!r} "
            "- the id must match the directory name"
        )
    return profile
