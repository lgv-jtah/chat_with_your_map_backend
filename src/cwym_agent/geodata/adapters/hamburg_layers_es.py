"""Layer catalog adapter for Hamburg's layer index (profile adapter
``hamburg_layers_es``): the generic Elasticsearch layer index at
``https://geodienste.hamburg.de/layers/_search`` (~6,500 layers as of
writing).
"""

from cwym_agent.geodata.adapters.es_layer_index import EsLayerIndexSource, _hit_to_document

__all__ = ["HamburgLayersEsSource", "_hit_to_document"]


class HamburgLayersEsSource(EsLayerIndexSource):
    """``LayerCatalogSource`` backed by Hamburg's layer index."""
