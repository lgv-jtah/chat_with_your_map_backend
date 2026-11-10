"""Tier 1 analysis: the fixed operations on one dataset (CHAT-11,
cmym-agent-architecture.md 14.4).

Pure computation - no I/O, no LLM. The features passed in are the
complete set the question is about (fetching them completely, or
refusing, is ``analysis_data``'s job); every function here works on all
of them.

The correctness rules this module carries:

- **Typed values (rule 2).** Values are converted by the attribute's
  declared type before sorting or computing: numbers accept "1962",
  "94,3" and "1.234,5"; ordinals sort by their declared order, never
  alphabetically.
- **Missing values are counted, not hidden (rule 3).** A value is
  missing when the property is absent, null or empty. A distribution
  shows them as their own bucket; top-N and the numeric aggregates skip
  them and report how many they skipped. A value that is present but
  can't be read as the declared type is reported separately, never
  silently treated as 0 or as missing.

The list of operations is closed (14.4): a new one is an architecture
decision, not a new branch here.
"""

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from cwym_agent.city_profile import AttributeConfig

Operation = Literal["count", "distribution", "top", "min", "max", "average", "sum", "lookup"]
OPERATIONS: tuple[str, ...] = ("count", "distribution", "top", "min", "max", "average", "sum", "lookup")
NEEDS_ATTRIBUTE = frozenset({"distribution", "top", "min", "max", "average", "sum"})
MAX_TOP = 20


class AnalysisError(ValueError):
    """The operation can't be computed for this attribute. ``reason`` is a
    stable code the reply templates turn into an honest sentence."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# --- typed values ------------------------------------------------------------

_MISSING = object()
_INVALID = object()

_PLAIN_NUMBER = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_GERMAN_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:\.\d{3})+(?:,\d+)?$")
_ENGLISH_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


def parse_number(raw: Any) -> float | None:
    """A number from a property value, or None if it isn't one.

    "1962" -> 1962, "94,3" -> 94.3, "1.234,5" -> 1234.5, "1,234.5" ->
    1234.5. A lone separator with exactly three digits after it ("1.234",
    "1,234") is read as a decimal separator - ambiguous, but declared
    number attributes of real datasets (years, floors, kWp) don't use
    thousands separators without decimals.
    """

    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else None
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace("\u00a0", "").replace(" ", "")
    if _PLAIN_NUMBER.match(text):
        return float(text.replace(",", "."))
    if _GERMAN_THOUSANDS.match(text):
        return float(text.replace(".", "").replace(",", "."))
    if _ENGLISH_THOUSANDS.match(text):
        return float(text.replace(",", ""))
    return None


def _is_missing(raw: Any) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _typed(raw: Any, attribute: AttributeConfig):
    """The value as the attribute's type, ``_MISSING`` or ``_INVALID``."""

    if _is_missing(raw):
        return _MISSING
    if attribute.type == "number":
        number = parse_number(raw)
        return _INVALID if number is None else number
    return str(raw).strip()


def _props(feature: dict) -> dict:
    return feature.get("properties") or {}


@dataclass(frozen=True)
class _Column:
    """One attribute over all features: typed values per feature."""

    values: list[Any]
    """Per feature: the typed value, ``_MISSING`` or ``_INVALID``."""

    @property
    def missing(self) -> int:
        return sum(v is _MISSING for v in self.values)

    @property
    def invalid(self) -> int:
        return sum(v is _INVALID for v in self.values)


def _column(features: Sequence[dict], name: str, attribute: AttributeConfig) -> _Column:
    return _Column([_typed(_props(f).get(name), attribute) for f in features])


def _ordinal_rank(attribute: AttributeConfig, value: str) -> int | None:
    order = attribute.order or []
    return order.index(value) if value in order else None


# --- results -------------------------------------------------------------------


@dataclass(frozen=True)
class Bucket:
    label: str
    """The value, bin ("1950–1959") or "" for the missing/other buckets."""
    count: int
    kind: Literal["value", "other", "missing"] = "value"
    color: str | None = None


@dataclass
class Computed:
    """What an operation computed - the numbers the reply must state
    unchanged (14.4 rule 4)."""

    operation: str
    total: int
    """Features the question is about (after filters and area)."""
    attribute: str | None = None
    buckets: list[Bucket] = field(default_factory=list)
    """distribution"""
    value: float | str | None = None
    """min/max/average/sum: the result; min/max of an ordinal: the value."""
    used: int = 0
    """Features whose value went into ``value``/``rows``."""
    missing: int = 0
    """Features without a value for the attribute (skipped)."""
    invalid: int = 0
    """Features whose value doesn't read as the declared type (skipped)."""
    rows: list[dict] = field(default_factory=list)
    """top / lookup / min / max: the features, in result order."""
    tie_beyond: int = 0
    """top: more features share the value of the last row."""
    unknown_values: list[str] = field(default_factory=list)
    """ordinal: values not in the declared order (counted, shown last)."""
    lookup_key: str | None = None
    nearby: list[str] = field(default_factory=list)
    """lookup without a match: what exists on that street."""


# --- operations ------------------------------------------------------------------


def count(features: Sequence[dict]) -> Computed:
    return Computed("count", total=len(features))


def distribution(features: Sequence[dict], name: str, attribute: AttributeConfig) -> Computed:
    column = _column(features, name, attribute)
    present = [v for v in column.values if v is not _MISSING and v is not _INVALID]
    result = Computed("distribution", total=len(features), attribute=name, missing=column.missing, invalid=column.invalid)
    colors = attribute.colors

    if attribute.type == "ordinal":
        counts = Counter(present)
        order = attribute.order or []
        buckets = [Bucket(v, counts[v], color=colors.get(v)) for v in order if counts[v]]
        unknown = sorted(v for v in counts if v not in order)
        buckets += [Bucket(v, counts[v]) for v in unknown]
        result.unknown_values = unknown
    elif attribute.type == "category":
        counts = Counter(present)
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if attribute.top is not None and len(ranked) > attribute.top:
            shown, rest = ranked[: attribute.top], ranked[attribute.top :]
            buckets = [Bucket(v, n, color=colors.get(v)) for v, n in shown]
            buckets.append(Bucket("", sum(n for _, n in rest), kind="other"))
        else:
            buckets = [Bucket(v, n, color=colors.get(v)) for v, n in ranked]
    else:
        buckets = _number_buckets(present, attribute)

    if column.missing:
        buckets.append(Bucket("", column.missing, kind="missing"))
    result.buckets = buckets
    return result


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _number_buckets(values: list[float], attribute: AttributeConfig) -> list[Bucket]:
    width = attribute.bin_width
    if width is None:
        counts = Counter(values)
        return [Bucket(_format_number(v), counts[v]) for v in sorted(counts)]
    counts: Counter = Counter(math.floor(v / width) for v in values)
    integer_bins = float(width).is_integer() and all(float(v).is_integer() for v in values)
    buckets = []
    for index in sorted(counts):
        low, high = index * width, (index + 1) * width
        label = (
            f"{_format_number(low)}–{_format_number(high - 1)}"
            if integer_bins
            else f"{_format_number(low)}–<{_format_number(high)}"
        )
        buckets.append(Bucket(label, counts[index], color=attribute.colors.get(label)))
    return buckets


def _sort_key(attribute: AttributeConfig):
    if attribute.type == "number":
        return lambda value: value
    if attribute.type == "ordinal":
        return lambda value: _ordinal_rank(attribute, value)
    raise AnalysisError("not_sortable", "category attributes have no order")


def top(
    features: Sequence[dict], name: str, attribute: AttributeConfig, n: int, descending: bool
) -> Computed:
    """The ``n`` features with the highest (``descending``) or lowest
    values. For an ordinal, "lowest" is the first value of its order."""

    if not 1 <= n <= MAX_TOP:
        raise AnalysisError("bad_n", f"n must be 1..{MAX_TOP}, got {n}")
    ranked, unknown, column = _ranked(features, name, attribute)
    # Stable and reproducible: ties keep the dataset's order.
    ranked.sort(key=lambda item: (-item[0] if descending else item[0], item[1]))
    chosen = ranked[:n]
    tie_beyond = 0
    if chosen and len(ranked) > n:
        last = chosen[-1][0]
        tie_beyond = sum(1 for k, _ in ranked[n:] if k == last)
    return Computed(
        "top",
        total=len(features),
        attribute=name,
        rows=[features[i] for _, i in chosen],
        used=len(ranked),
        missing=column.missing,
        invalid=column.invalid + sum(unknown.values()),
        tie_beyond=tie_beyond,
        unknown_values=sorted(unknown),
    )


def _ranked(features: Sequence[dict], name: str, attribute: AttributeConfig):
    """(sort key, feature index) for every feature with a sortable value;
    ordinal values outside the declared order (value -> count); the
    column."""

    key = _sort_key(attribute)
    column = _column(features, name, attribute)
    ranked, unknown = [], Counter()
    for index, value in enumerate(column.values):
        if value is _MISSING or value is _INVALID:
            continue
        sort_key = key(value)
        if sort_key is None:
            unknown[value] += 1
        else:
            ranked.append((sort_key, index))
    return ranked, unknown, column


def extreme(features: Sequence[dict], name: str, attribute: AttributeConfig, operation: str) -> Computed:
    """min / max: the value and every feature that has it."""

    ranked, unknown, column = _ranked(features, name, attribute)
    result = Computed(
        operation, total=len(features), attribute=name, missing=column.missing,
        invalid=column.invalid + sum(unknown.values()), used=len(ranked), unknown_values=sorted(unknown),
    )
    if not ranked:
        return result
    best = (max if operation == "max" else min)(k for k, _ in ranked)
    indexes = [i for k, i in ranked if k == best]
    result.value = column.values[indexes[0]]
    result.rows = [features[i] for i in indexes]
    return result


def aggregate(features: Sequence[dict], name: str, attribute: AttributeConfig, operation: str) -> Computed:
    """average / sum over number attributes."""

    if attribute.type != "number":
        raise AnalysisError("not_numeric", f"{name} is {attribute.type}")
    column = _column(features, name, attribute)
    numbers = [v for v in column.values if v is not _MISSING and v is not _INVALID]
    result = Computed(operation, total=len(features), attribute=name, missing=column.missing, invalid=column.invalid)
    result.used = len(numbers)
    if numbers:
        result.value = sum(numbers) if operation == "sum" else sum(numbers) / len(numbers)
    return result


def compute(
    operation: str,
    features: Sequence[dict],
    name: str | None = None,
    attribute: AttributeConfig | None = None,
    n: int = 5,
    descending: bool = True,
) -> Computed:
    """Dispatch one of ``OPERATIONS`` (lookup has its own entry point)."""

    if operation == "count":
        return count(features)
    if operation not in NEEDS_ATTRIBUTE:
        raise AnalysisError("unknown_operation", operation)
    if name is None or attribute is None:
        raise AnalysisError("no_attribute", operation)
    if operation == "distribution":
        return distribution(features, name, attribute)
    if operation == "top":
        return top(features, name, attribute, n, descending)
    if operation in ("min", "max"):
        return extreme(features, name, attribute, operation)
    return aggregate(features, name, attribute, operation)


# --- lookup by address -----------------------------------------------------------

_HOUSE_NUMBER = r"\d+\s*[a-z]?"
_ADDRESS = re.compile(rf"^(?P<street>.*?)\s*(?P<numbers>{_HOUSE_NUMBER}(?:\s*[/,-]\s*{_HOUSE_NUMBER})*)?$")


def _fold(text: str) -> str:
    """"Hugo-Wolf-Str. 57" and "hugo wolf strasse 57" alike: lowercase, ß
    -> ss, "str."/"straße" -> "strasse", hyphens between letters ->
    spaces (a hyphen between numbers is a range: "8-10")."""

    text = unicodedata.normalize("NFC", text).lower().replace("ß", "ss")
    text = re.sub(r"str\.?(?=\s|\d|$)", "strasse", text)
    text = re.sub(r"(?<=[^\W\d_])-(?=[^\W\d_])", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.")


def parse_address(text: str) -> tuple[str, list[str]]:
    """("street", ["8", "10"]) from "Lindenweg 8/10"; numbers lowercased
    without spaces ("12 a" -> "12a")."""

    folded = _fold(text)
    match = _ADDRESS.match(folded)
    street = (match.group("street") if match else folded).strip()
    numbers = match.group("numbers") if match else None
    parts = [re.sub(r"\s+", "", p) for p in re.split(r"[/,-]", numbers)] if numbers else []
    return street, [p for p in parts if p]


def lookup(features: Sequence[dict], lookup_field: str, key: str) -> Computed:
    """Every feature at the address ``key`` (house-number aware), or every
    feature on the street when no number is given.

    No substring guessing: "Lindenweg 1" doesn't find Lindenweg 17
    (ASCEND's bug), but returns nothing and lists the numbers that do
    exist on the street (``nearby``). An address shared by several
    features returns all of them; "Lindenweg 8/10" is found by 8 and 10.
    """

    street, numbers = parse_address(key)
    result = Computed("lookup", total=len(features), lookup_key=key)
    if not street:
        return result
    on_street = []
    for feature in features:
        raw = _props(feature).get(lookup_field)
        if _is_missing(raw):
            continue
        f_street, f_numbers = parse_address(str(raw))
        if f_street == street:
            on_street.append((feature, f_numbers, str(raw).strip()))
    if not numbers:
        result.rows = [f for f, _, _ in on_street]
    else:
        wanted = set(numbers)
        result.rows = [f for f, f_numbers, _ in on_street if wanted & set(f_numbers)]
        if not result.rows:
            result.nearby = sorted({raw for _, _, raw in on_street}, key=_natural_key)
    result.used = len(result.rows)
    return result


def _natural_key(text: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text.lower())]


def feature_ids(features: Iterable[dict], id_field: str) -> list[str]:
    return [str(_props(f).get(id_field, f.get("id"))) for f in features]
