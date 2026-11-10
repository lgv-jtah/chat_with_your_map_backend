"""Layer catalog adapter for Hamburg's layer index (profile adapter
``hamburg_layers_es``).

Fetches the full map layer catalog from Hamburg's public,
unauthenticated Elasticsearch endpoint
(``https://geodienste.hamburg.de/layers/_search``, ~6,500 layers as of
writing) and turns each layer into a Haystack document for
``LayerSearchService``.
"""

import logging
import time

import httpx
from haystack import Document

logger = logging.getLogger(__name__)


class HamburgLayersEsSource:
    """``LayerCatalogSource`` backed by Hamburg's layer index."""

    def __init__(
        self,
        url: str,
        http_client: httpx.Client,
        fetch_size: int = 10000,
        retries: int = 5,
        retry_backoff: float = 2.0,
    ):
        self.url = url
        self.fetch_size = fetch_size
        self.retries = retries
        self.retry_backoff = retry_backoff
        self._client = http_client

    def fetch_documents(self) -> list[Document]:
        """Fetch the full layer catalog and convert it to Documents.

        Retries on transient failures (this endpoint has been observed to
        intermittently time out, seemingly unrelated to payload size - some
        requests complete in under a second, others hang until the client
        timeout) since this fetch happens once at application startup and a
        single flaky request shouldn't crash the whole app.
        """

        logger.info("Fetching the layer catalog from %s ...", self.url)
        attempts = max(1, self.retries)

        last_exc: httpx.HTTPError | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.get(self.url, params={"q": "*:*", "size": self.fetch_size})
                response.raise_for_status()
                es_results = response.json()
                break
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "Layer catalog fetch attempt %d/%d failed (%s); %s",
                    attempt,
                    attempts,
                    exc,
                    "retrying" if attempt < attempts else "giving up",
                )
                if attempt < attempts:
                    time.sleep(self.retry_backoff)
        else:
            assert last_exc is not None
            raise last_exc

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
            "collection": source.get("collection"),
            "short_description": short_description,
        },
    )
