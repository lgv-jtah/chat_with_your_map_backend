"""Hybrid semantic search over a city's map layer catalog.

Indexes the documents of the city's ``LayerCatalogSource`` (thousands of
layers) and serves hybrid BM25 + embedding retrieval, cross-encoder
reranked, semantic search over them - so loose terms like
"Fahrradstationen" find the official layer name among the candidates.

This mirrors the retrieval pattern used by the sister `udp-chatbot`
project's `mcp_server/search.py` (hybrid BM25 + embedding retrieval,
joined, cross-encoder reranked) but against an in-memory Haystack
document store instead of OpenSearch: at ~6,500 short documents,
in-memory search is comfortably fast enough, and this avoids running a
whole extra service just for this.
"""

import logging
from collections.abc import Collection

from haystack import Document, Pipeline
from haystack.components.joiners import DocumentJoiner
from haystack.components.retrievers.in_memory import InMemoryBM25Retriever, InMemoryEmbeddingRetriever
from haystack.document_stores.in_memory import InMemoryDocumentStore
from haystack_integrations.components.embedders.sentence_transformers import (
    SentenceTransformersDocumentEmbedder,
    SentenceTransformersTextEmbedder,
)
from haystack_integrations.components.rankers.sentence_transformers import SentenceTransformersSimilarityRanker

from cwym_agent.geodata.interfaces import LayerCatalogSource
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import LayerMatch

logger = logging.getLogger(__name__)


def _layer_id_filter(allowed_layer_ids: Collection[str] | None) -> dict | None:
    """Build the Haystack document-store filter that scopes retrieval to
    ``allowed_layer_ids`` (``PortalCatalogResolver``, Section 8), or
    ``None`` for an unscoped search."""

    if allowed_layer_ids is None:
        return None

    return {"field": "meta.layer_id", "operator": "in", "value": [str(i) for i in allowed_layer_ids]}


class LayerSearchService:
    """Hybrid BM25 + semantic search over a city's map layer catalog."""

    def __init__(
        self,
        source: LayerCatalogSource | None = None,
        settings: GeodataSettings | None = None,
        documents: list[Document] | None = None,
    ):
        """
        :param source: where the catalog comes from (fetched on warm-up).
        :param documents: Pre-fetched layer documents to index instead of
            fetching them from ``source`` - used by tests to avoid
            depending on network access.
        """

        if source is None and documents is None:
            raise ValueError("LayerSearchService needs a catalog source or documents")
        self.source = source
        self.settings = settings or GeodataSettings()
        self._preloaded_documents = documents
        self._pipeline: Pipeline | None = None

    def warm_up(self) -> None:
        """Fetch the layer catalog, embed it, and build the search pipeline.

        Expensive (one fetch of thousands of documents + embedding all of
        them) - call once, e.g. at application startup, not per request.
        Idempotent: safe to call more than once.
        """

        if self._pipeline is not None:
            return

        documents = self._preloaded_documents
        if documents is None:
            assert self.source is not None
            documents = self.source.fetch_documents()
        logger.info(
            "Embedding %d layer documents with %s - this takes a few "
            "minutes on CPU (first run also downloads the model) ...",
            len(documents),
            self.settings.layer_embed_model,
        )

        document_store = InMemoryDocumentStore(embedding_similarity_function="cosine")

        doc_embedder = SentenceTransformersDocumentEmbedder(model=self.settings.layer_embed_model)
        doc_embedder.warm_up()
        embedded_documents = doc_embedder.run(documents=documents)["documents"]
        document_store.write_documents(embedded_documents)
        logger.info("Finished embedding %d layer documents", len(embedded_documents))

        pipeline = Pipeline()
        pipeline.add_component(
            "bm25_retriever",
            InMemoryBM25Retriever(
                document_store=document_store, top_k=self.settings.layer_retriever_top_k
            ),
        )
        pipeline.add_component(
            "query_embedder",
            SentenceTransformersTextEmbedder(model=self.settings.layer_embed_model),
        )
        pipeline.add_component(
            "embedding_retriever",
            InMemoryEmbeddingRetriever(
                document_store=document_store, top_k=self.settings.layer_retriever_top_k
            ),
        )
        pipeline.add_component("document_joiner", DocumentJoiner(join_mode="concatenate"))
        pipeline.add_component(
            "ranker",
            SentenceTransformersSimilarityRanker(
                model=self.settings.layer_ranker_model,
                top_k=self.settings.layer_result_top_k,
                scale_score=True,
            ),
        )

        pipeline.connect("bm25_retriever.documents", "document_joiner.documents")
        pipeline.connect("query_embedder.embedding", "embedding_retriever.query_embedding")
        pipeline.connect("embedding_retriever.documents", "document_joiner.documents")
        pipeline.connect("document_joiner.documents", "ranker.documents")

        pipeline.warm_up()
        self._pipeline = pipeline
        logger.info("Layer search index ready (%d documents indexed)", len(embedded_documents))

    def search(self, query: str, allowed_layer_ids: Collection[str] | None = None) -> list[LayerMatch]:
        """Return the best-matching layers for ``query``, best first.

        Returns a broader candidate pool (``layer_result_top_k``, default
        15) rather than a single best guess: text/embedding similarity
        alone cannot reliably bridge colloquial terms to official
        brand names with no lexical overlap (e.g. "Fahrradstationen" vs.
        "StadtRAD") - see ``RealDataServices._select_layer_with_llm``,
        which disambiguates the final choice among these candidates using
        Qwen3's world knowledge.

        :param allowed_layer_ids: when given, restricts retrieval to
            these layer ids (``PortalCatalogResolver``, Section 8) rather
            than searching the full city-wide index.
        """

        self.warm_up()
        assert self._pipeline is not None

        filters = _layer_id_filter(allowed_layer_ids)
        result = self._pipeline.run(
            {
                "bm25_retriever": {"query": query, "filters": filters},
                "query_embedder": {"text": query},
                "embedding_retriever": {"filters": filters},
                "ranker": {"query": query},
            }
        )
        documents = result.get("ranker", {}).get("documents", [])

        return [
            LayerMatch(
                name=doc.meta.get("layer_name", "Unbekannte Ebene"),
                layer_id=doc.meta.get("layer_id", ""),
                confidence=doc.score if doc.score is not None else 0.0,
                description=doc.meta.get("short_description", ""),
                typ=doc.meta.get("typ"),
                url=doc.meta.get("url"),
                collection=doc.meta.get("collection"),
            )
            for doc in documents
        ]

    def resolve_layer(self, query: str) -> LayerMatch | None:
        """Return the single best-matching layer for ``query``, if any."""

        matches = self.search(query)
        return matches[0] if matches else None
