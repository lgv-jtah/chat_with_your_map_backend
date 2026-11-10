"""Tier 1 operations (CHAT-11, architecture 14.4) on the fixture's 12
buildings. Expected values are counted by hand from
tests/fixtures/buildings.geojson."""

import json

import pytest
from fixtures.datasets import BUILDINGS, FIXTURES

from cwym_agent.city_profile import AttributeConfig
from cwym_agent.geodata import analysis
from cwym_agent.geodata.analysis import AnalysisError, Bucket, compute, lookup, parse_number

FEATURES = json.loads((FIXTURES / "buildings.geojson").read_text(encoding="utf-8"))["features"]
ATTRS = {name: AttributeConfig.model_validate(a) for name, a in BUILDINGS["attributes"].items()}


def ids(rows):
    return [row["properties"]["gebid"] for row in rows]


def run(operation, attribute=None, **kwargs):
    return compute(operation, FEATURES, attribute, ATTRS.get(attribute), **kwargs)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1962", 1962.0), ("94,3", 94.3), ("12.5", 12.5), (" 120 ", 120.0), ("1.234,5", 1234.5),
        ("1,234.5", 1234.5), ("-3", -3.0), (7, 7.0), (2.5, 2.5),
        ("unbekannt", None), ("", None), (None, None), (True, None), ("12 kWp", None), (float("nan"), None),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


class TestCount:
    def test_counts_every_feature(self):
        assert run("count").total == 12


class TestDistribution:
    def test_ordinal_follows_the_declared_order_and_counts_missing(self):
        result = run("distribution", "energieeffizienzklasse")

        assert [(b.label, b.count, b.kind) for b in result.buckets] == [
            ("A+", 1, "value"), ("A", 1, "value"), ("B", 1, "value"), ("C", 1, "value"), ("E", 1, "value"),
            ("F", 1, "value"), ("G", 1, "value"), ("H", 3, "value"), ("", 2, "missing"),
        ]
        assert result.missing == 2
        assert sum(b.count for b in result.buckets) == result.total == 12
        assert result.buckets[0].color == "#16a34a" and result.buckets[7].color == "#991b1b"

    def test_category_top_k_plus_other(self):
        result = run("distribution", "eigentuemer")

        assert result.buckets == [Bucket("Wohnbau", 7), Bucket("Stadt", 3), Bucket("", 2, kind="other")]

    def test_number_bins(self):
        result = run("distribution", "baujahr")

        assert [(b.label, b.count) for b in result.buckets] == [
            ("1940–1949", 1), ("1950–1959", 5), ("1960–1969", 2), ("1970–1979", 1),
            ("2000–2009", 1), ("2010–2019", 1), ("2020–2029", 1),
        ]

    def test_number_without_bins_sorts_numerically(self):
        result = run("distribution", "geschosse")

        assert [b.label for b in result.buckets] == ["1", "2", "3", "4", "9"]

    def test_unknown_ordinal_values_are_counted_and_named(self):
        features = [*FEATURES, {"properties": {"energieeffizienzklasse": "X"}}]

        result = compute("distribution", features, "energieeffizienzklasse", ATTRS["energieeffizienzklasse"])

        assert result.unknown_values == ["X"]
        assert result.buckets[-2] == Bucket("X", 1)
        assert sum(b.count for b in result.buckets) == 13


class TestTop:
    def test_newest(self):
        result = run("top", "baujahr", n=3, descending=True)

        assert ids(result.rows) == ["B12", "B08", "B09"]
        assert result.tie_beyond == 0

    def test_oldest_reports_a_tie_at_the_cut(self):
        result = run("top", "baujahr", n=2, descending=False)

        assert ids(result.rows) == ["B06", "B01"]
        assert result.tie_beyond == 1  # B02 is also from 1952

    def test_numbers_with_decimal_commas_sort_as_numbers(self):
        """ASCEND sorted "120" < "2,2" as strings."""

        result = run("top", "pv_leistung", n=3, descending=True)

        assert ids(result.rows) == ["B04", "B08", "B02"]
        assert (result.used, result.missing) == (4, 8)

    def test_ordinal_best_first(self):
        result = run("top", "energieeffizienzklasse", n=2, descending=False)

        assert ids(result.rows) == ["B08", "B09"]  # A+, A
        assert result.missing == 2

    def test_category_has_no_order(self):
        with pytest.raises(AnalysisError) as e:
            run("top", "eigentuemer", n=3)
        assert e.value.reason == "not_sortable"

    @pytest.mark.parametrize("n", [0, analysis.MAX_TOP + 1])
    def test_n_is_bounded(self, n):
        with pytest.raises(AnalysisError) as e:
            run("top", "baujahr", n=n)
        assert e.value.reason == "bad_n"


class TestExtremes:
    def test_max_returns_every_feature_with_the_value(self):
        result = run("max", "geschosse")

        assert result.value == 9 and ids(result.rows) == ["B07", "B08"]

    def test_ordinal_min_is_the_first_value_of_the_order(self):
        result = run("min", "energieeffizienzklasse")
        worst = run("max", "energieeffizienzklasse")

        assert result.value == "A+" and ids(result.rows) == ["B08"]
        assert worst.value == "H" and ids(worst.rows) == ["B01", "B02", "B06"]
        assert worst.missing == 2

    def test_nothing_to_compute(self):
        result = compute("max", [{"properties": {}}], "baujahr", ATTRS["baujahr"])

        assert result.value is None and result.missing == 1


class TestAggregates:
    def test_sum_and_average_skip_and_report_missing(self):
        total = run("sum", "pv_leistung")
        mean = run("average", "pv_leistung")

        assert total.value == pytest.approx(229.0)  # 12.5 + 120 + 2.2 + 94.3
        assert mean.value == pytest.approx(57.25)
        assert (mean.used, mean.missing, mean.invalid) == (4, 8, 0)

    def test_unreadable_values_are_reported_not_zero(self):
        features = [{"properties": {"baujahr": "1950"}}, {"properties": {"baujahr": "unbekannt"}}]

        result = compute("average", features, "baujahr", ATTRS["baujahr"])

        assert result.value == 1950 and (result.used, result.invalid) == (1, 1)

    def test_only_for_numbers(self):
        with pytest.raises(AnalysisError) as e:
            run("average", "energieeffizienzklasse")
        assert e.value.reason == "not_numeric"


class TestLookup:
    @pytest.mark.parametrize(
        "key, expected",
        [
            ("Lindenweg 1", ["B01"]),  # not Lindenweg 17 (ASCEND's substring bug)
            ("Lindenweg 17", ["B04"]),
            ("lindenweg 3", ["B02"]),
            ("Birkenstraße 4", ["B07", "B08"]),  # a shared address: all of them
            ("Birkenstr. 4", ["B07", "B08"]),
            ("Lindenweg 10", ["B05"]),  # "Lindenweg 8/10"
            ("Lindenweg 8", ["B05"]),
            ("Schleissheimer Strasse 450", ["B12"]),
            ("Birkenstraße", ["B06", "B07", "B08", "B09"]),  # street only
        ],
    )
    def test_matches(self, key, expected):
        assert ids(lookup(FEATURES, "adresse", key).rows) == expected

    def test_unknown_number_lists_what_exists_on_the_street(self):
        result = lookup(FEATURES, "adresse", "Lindenweg 7")

        assert result.rows == []
        assert result.nearby == ["Lindenweg 1", "Lindenweg 3", "Lindenweg 5", "Lindenweg 8/10", "Lindenweg 17"]

    def test_unknown_street(self):
        result = lookup(FEATURES, "adresse", "Eichenallee 1")

        assert result.rows == [] and result.nearby == []


def test_unknown_operation():
    with pytest.raises(AnalysisError):
        run("median", "baujahr")
