"""Hybrid semantic search over Hamburg's Masterportal map layer catalog.

Fetches the full layer catalog from Hamburg's public, unauthenticated
Elasticsearch endpoint (``https://geodienste.hamburg.de/layers/_search``,
~6,500 layers as of writing) and serves hybrid BM25 + embedding
retrieval, cross-encoder reranked, semantic search over it - so loose
German terms like "Fahrradstationen" resolve to the correct official
layer name ("StadtRAD-Stationen Hamburg").

This mirrors the retrieval pattern used by the sister `udp-chatbot`
project's `mcp_server/search.py` (hybrid BM25 + embedding retrieval,
joined, cross-encoder reranked) but against an in-memory Haystack
document store instead of OpenSearch: at ~6,500 short documents,
in-memory search is comfortably fast enough, and this avoids running a
whole extra service just for this.
"""

import logging

import httpx
from haystack import Document, Pipeline
from haystack.components.joiners import DocumentJoiner
from haystack.components.retrievers.in_memory import InMemoryBM25Retriever, InMemoryEmbeddingRetriever
from haystack.document_stores.in_memory import InMemoryDocumentStore
from haystack_integrations.components.embedders.sentence_transformers import (
    SentenceTransformersDocumentEmbedder,
    SentenceTransformersTextEmbedder,
)
from haystack_integrations.components.rankers.sentence_transformers import SentenceTransformersSimilarityRanker

from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import LayerMatch

logger = logging.getLogger(__name__)


def fetch_layer_documents(
    settings: GeodataSettings, http_client: httpx.Client | None = None
) -> list[Document]:
    """Fetch the full Hamburg layer catalog and convert it to Documents."""

    client = http_client or httpx.Client(timeout=settings.timeout)
    response = client.get(
        settings.layer_search_url,
        params={"q": "*:*", "size": settings.layer_fetch_size},
    )
    response.raise_for_status()
    es_results = response.json()
    hits = es_results.get("hits", {}).get("hits", [])

    documents = []
    for hit in hits:
        doc = _hit_to_document(hit.get("_source") or {})
        if doc is not None:
            documents.append(doc)

    return documents


def _hit_to_document(source: dict) -> Document | None:
    layer_id = source.get("id")
    layer_name = source.get("name")
    if layer_id is None or not layer_name:
        return None

    content_parts = [
        f"Layer: {layer_name}",
        f"ID: {layer_id}",
        f"Typ: {source.get('typ', 'Unbekannt')}",
    ]

    short_description = ""
    datasets = source.get("datasets") or []
    if datasets:
        dataset = datasets[0]
        if dataset.get("md_name"):
            content_parts.append(f"Metadatenname: {dataset['md_name']}")
        if dataset.get("description"):
            description = dataset["description"]
            content_parts.append(f"Beschreibung: {description}")
            # A short excerpt for LLM disambiguation prompts - the full
            # description is often several paragraphs of licensing/technical
            # detail that would bloat those prompts unnecessarily.
            short_description = description.strip().split("\n")[0][:200]
        keywords = dataset.get("keywords") or []
        if keywords:
            content_parts.append(f"Schlagwörter: {', '.join(keywords)}")

    content = "\n".join(content_parts)

    return Document(
        content=content,
        meta={
            "layer_id": str(layer_id),
            "layer_name": layer_name,
            "typ": source.get("typ"),
            "url": source.get("url"),
            "short_description": short_description,
        },
    )


class LayerSearchService:
    """Hybrid BM25 + semantic search over Hamburg's map layer catalog."""

    def __init__(
        self,
        settings: GeodataSettings | None = None,
        http_client: httpx.Client | None = None,
        documents: list[Document] | None = None,
    ):
        """
        :param documents: Pre-fetched layer documents to index instead of
            fetching them from the live endpoint - used by tests to
            avoid depending on network access.
        """

        self.settings = settings or GeodataSettings()
        self._http_client = http_client
        self._preloaded_documents = documents
        self._pipeline: Pipeline | None = None

    def warm_up(self) -> None:
        """Fetch the layer catalog, embed it, and build the search pipeline.

        Expensive (one HTTP fetch of ~6,500 documents + embedding all of
        them) - call once, e.g. at application startup, not per request.
        Idempotent: safe to call more than once.
        """

        if self._pipeline is not None:
            return

        documents = self._preloaded_documents
        if documents is None:
            documents = fetch_layer_documents(self.settings, http_client=self._http_client)
        logger.info("Indexing %d Hamburg layer documents", len(documents))

        document_store = InMemoryDocumentStore(embedding_similarity_function="cosine")

        doc_embedder = SentenceTransformersDocumentEmbedder(model=self.settings.layer_embed_model)
        doc_embedder.warm_up()
        embedded_documents = doc_embedder.run(documents=documents)["documents"]
        document_store.write_documents(embedded_documents)

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

    def search(self, query: str) -> list[LayerMatch]:
        """Return the best-matching layers for ``query``, best first.

        Returns a broader candidate pool (``layer_result_top_k``, default
        15) rather than a single best guess: text/embedding similarity
        alone cannot reliably bridge colloquial terms to official Hamburg
        brand names with no lexical overlap (e.g. "Fahrradstationen" vs.
        "StadtRAD") - see ``RealDataServices._select_layer_with_llm``,
        which disambiguates the final choice among these candidates using
        Qwen3's world knowledge.
        """

        self.warm_up()
        assert self._pipeline is not None

        result = self._pipeline.run(
            {
                "bm25_retriever": {"query": query},
                "query_embedder": {"text": query},
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
            )
            for doc in documents
        ]

    def resolve_layer(self, query: str) -> LayerMatch | None:
        """Return the single best-matching layer for ``query``, if any."""

        matches = self.search(query)
        return matches[0] if matches else None
