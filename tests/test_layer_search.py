"""Unit tests for ``layer_search.py`` that need no embedding model.
The catalog fetch is tested with its adapter (test_hamburg_layers_es.py).
"""

import pytest

from cwym_agent.geodata.layer_search import LayerSearchService, _layer_id_filter


class TestLayerIdFilter:
    def test_none_allowed_ids_means_unscoped(self):
        assert _layer_id_filter(None) is None

    def test_builds_haystack_in_filter(self):
        result = _layer_id_filter({"27764", "18105"})

        assert result["field"] == "meta.layer_id"
        assert result["operator"] == "in"
        assert sorted(result["value"]) == ["18105", "27764"]

    def test_coerces_ids_to_strings(self):
        result = _layer_id_filter([1234])

        assert result["value"] == ["1234"]


def test_needs_a_source_or_documents():
    with pytest.raises(ValueError):
        LayerSearchService()

