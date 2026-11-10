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

CHAT-3 scope: the profile is loaded, validated and passed into
``build_pipeline``; the components don't read from it yet (CHAT-5 moves
the Hamburg-specific code behind interfaces fed by it). Until then
``tests/test_city_profile.py`` checks that the Hamburg profile matches the
values still hard-coded in the code, so the two can't drift apart.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
    place_types: dict[str, str] = Field(min_length=1)


class LayerCatalogConfig(_Strict):
    adapter: Literal["hamburg_layers_es"]
    url: HttpUrlStr


class BoundaryCollection(_Strict):
    collection: str = Field(min_length=1)
    name_field: str = Field(min_length=1)


class BoundariesConfig(_Strict):
    adapter: Literal["ogc_api_features"]
    url: HttpUrlStr
    # Prefixes the address search puts on some area names that the
    # boundary dataset doesn't have ("Hamburg-Harburg" -> "Harburg").
    strip_name_prefixes: list[str] = Field(default_factory=list)
    # CWYM location kind -> collection holding its official outlines.
    collections: dict[str, BoundaryCollection] = Field(min_length=1)


class AnalysisAgentConfig(_Strict):
    """Tier 2 analysis agent (Section 14.4). Endpoint and timeouts stay in
    environment variables (CWYM_UDP_AGENT_*), they differ per environment."""

    adapter: Literal["udp"]


class CityProfile(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    """Short city name used in prompts and replies, e.g. "Hamburg"."""
    official_name: str = Field(min_length=1)
    languages: list[str] = Field(min_length=1)
    """Supported answer languages, ISO 639-1; the first is the default."""
    default_location: DefaultLocation
    extent: tuple[float, float, float, float]
    """WGS84 (minLon, minLat, maxLon, maxLat): the 'whole city' map view."""
    geocoder: GeocoderConfig
    layer_catalog: LayerCatalogConfig
    boundaries: BoundariesConfig
    analysis_agent: AnalysisAgentConfig | None = None
    """None: the city has no tier 2 analysis agent."""

    @field_validator("languages")
    @classmethod
    def _languages(cls, v: list[str]) -> list[str]:
        for code in v:
            if len(code) != 2 or not code.isalpha() or not code.islower():
                raise ValueError(f"language codes must be two lowercase letters (ISO 639-1), got {code!r}")
        if len(set(v)) != len(v):
            raise ValueError("language codes must be unique")
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
        return self

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
        profile = CityProfile.model_validate(data)
    except ValidationError as e:
        raise CityProfileError(f"Invalid city profile {path}:\n{_format_errors(e)}") from None

    if expected_id is not None and profile.id != expected_id:
        raise CityProfileError(
            f"City profile {path} declares id {profile.id!r} but is stored as {expected_id!r} "
            "- the id must match the directory name"
        )
    return profile
