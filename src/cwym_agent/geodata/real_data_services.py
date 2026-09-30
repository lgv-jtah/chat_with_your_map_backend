"""Real implementation of ``StubDataServices``'s interface.

Combines ``AddressResolver`` (location -> coordinates) and
``LayerSearchService`` (loose term -> official layer name) behind the
same ``resolve_locations``/``resolve_layer``/``detect_action`` interface
``AgentExecutorComponent`` already calls, so it needs no changes to use
either implementation.

Layer resolution needs two LLM-assisted steps on top of
``LayerSearchService``'s retrieval, not just retrieval alone: text/
embedding similarity cannot bridge colloquial German terms to Hamburg's
official layer names when they share no lexical overlap - e.g.
"Fahrradstationen" scores far closer to "Fahrradluftstationen" (bike
air-pump stations) than to the actually-correct "StadtRAD-Stationen
Hamburg" (Hamburg's bike-sharing brand name) under both embedding
cosine similarity and cross-encoder reranking, and "StadtRAD" doesn't
even make the top ~40 retrieved candidates for that query - verified
empirically against the real layer catalog. Qwen3 already has the
world knowledge to make this connection (it did so unprompted during
earlier pipeline testing), so:

1. Qwen3 first proposes likely official term(s) for the query (query
   expansion, e.g. "Fahrradstationen" -> "StadtRAD"); those terms are
   searched too, alongside the original query, and results merged -
   searching "StadtRAD" directly resolves it with ~0.97 confidence.
2. Qwen3 then picks the actual best match from the merged candidate
   shortlist (disambiguation), since expansion alone can still surface
   multiple plausible candidates.
"""

import logging
import re
from collections.abc import Collection
from typing import Any

from cwym_agent.geodata.address_resolver import AddressResolver
from cwym_agent.geodata.filter_agent import FilterAgent
from cwym_agent.geodata.layer_search import LayerSearchService
from cwym_agent.geodata.settings import GeodataSettings
from cwym_agent.geodata.types import (
    ActionMatch,
    AnalysisResult,
    FilterResult,
    LayerMatch,
    LocationMatch,
)
from cwym_agent.geodata.udp_analysis import UdpAnalysisClient
from cwym_agent.llm.qwen3_client import Qwen3Client

logger = logging.getLogger(__name__)

# Hamburg's own address index (api.hamburg.de/addr_search) is scoped to
# streets/districts/boroughs *within* Hamburg - it has no single record
# for "Hamburg" the city itself (confirmed live: searching "Hamburg" or
# "Freie und Hansestadt Hamburg" returns only its individual boroughs,
# e.g. "Hamburg-Mitte", "Hamburg-Nord", never a city-wide hit). Whenever
# no more specific location is resolved (unmatched query, or the query
# genuinely is just "Hamburg"), fall back to the city's well-known
# center, matching what StubDataServices did for its equivalent default
# case.
#
# _HAMBURG_EXTENT is chat_with_your_map_portal/config.json's own
# portalConfig.map.mapView.extent - [510000.0, 5850000.0, 625000.4,
# 6000000.0] in EPSG:25832 - converted to WGS84 via pyproj
# (Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)),
# not a from-scratch guess: reuses the portal author's own already-
# vetted "sensible whole-region view", not a data-derived union of
# Hamburg's boroughs (which would technically be more "correct" but
# includes far-flung exclaves like Neuwerk, an island in the North Sea
# that's administratively part of Hamburg-Mitte - zooming out that far
# would make the actual urban area look tiny, the opposite of what a
# "show me Hamburg" default should look like).
_HAMBURG_EXTENT = (9.148322816744086, 52.79970456940332, 10.913170524761878, 54.13290152266379)

_DEFAULT_LOCATION = LocationMatch("Hamburg", 53.5511, 9.9937, 0.9, extent=_HAMBURG_EXTENT)

_ACTION_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("navigiere", "navigate", "fahre nach"), "Navigieren"),
    (("filter", "filtere"), "Filter"),
    (("öffne", "oeffne", "open"), "Öffne"),
]
_DEFAULT_ACTION = "Zeigen"

_LAYER_SELECTION_PROMPT_TEMPLATE = (
    "Du hilfst dabei, aus einer Liste von Kandidaten-Kartenebenen die am "
    "besten passende für eine Nutzeranfrage auszuwählen. Der Kontext ist "
    "ausschließlich die Freie und Hansestadt Hamburg - bevorzuge Ebenen, "
    "die sich auf Hamburg beziehen, gegenüber gleichnamigen Ebenen zu "
    "anderen Bundesländern oder Regionen (z.B. Niedersachsen, "
    "Schleswig-Holstein). Antworte NUR mit der Nummer der am besten "
    "passenden Ebene. Antworte mit 0, wenn keine der Ebenen wirklich "
    "passt.\n\n"
    "Anfrage: {query}\n\n"
    "Kandidaten:\n{candidates}"
)

_QUERY_EXPANSION_PROMPT_TEMPLATE = (
    "Du hilfst dabei, für eine Nutzeranfrage passende Suchbegriffe für "
    "Hamburgs offiziellen Geodaten-Katalog zu finden. Nenne bis zu 3 "
    "mögliche offizielle Bezeichnungen, Markennamen oder Fachbegriffe, "
    "unter denen ein passender Datensatz dort tatsächlich geführt sein "
    "könnte (z.B. \"StadtRAD\" für \"Fahrradstationen\", weil das "
    "Hamburger Bike-Sharing-System offiziell so heißt). Antworte NUR mit "
    "den Begriffen, kommagetrennt, ohne weitere Erklärung.\n\n"
    "Anfrage: {query}"
)


def _format_candidates(candidates: list[LayerMatch]) -> str:
    lines = []
    for index, candidate in enumerate(candidates, start=1):
        line = f"{index}. {candidate.name}"
        if candidate.description:
            line += f" - {candidate.description}"
        lines.append(line)
    return "\n".join(lines)


def _parse_selection_index(reply: str, num_candidates: int) -> int | None:
    """Extract the candidate number from an LLM reply, if parseable.

    Returns ``None`` on anything unparseable/out of range, so the caller
    can fail safe rather than trust a malformed response.
    """

    match = re.search(r"-?\d+", reply)
    if match is None:
        return None

    value = int(match.group())
    if value < 0 or value > num_candidates:
        return None

    return value


class RealDataServices:
    """Real location/layer/action resolution, backed by Hamburg's public APIs."""

    def __init__(
        self,
        settings: GeodataSettings | None = None,
        address_resolver: AddressResolver | None = None,
        layer_search: LayerSearchService | None = None,
        filter_agent: FilterAgent | None = None,
        llm: Any | None = None,
        udp_analysis: UdpAnalysisClient | None = None,
    ):
        self.settings = settings or GeodataSettings()
        self.address_resolver = address_resolver or AddressResolver(self.settings)
        self.layer_search = layer_search or LayerSearchService(self.settings)
        self.llm = llm if llm is not None else Qwen3Client()
        self.filter_agent = filter_agent or FilterAgent(self.settings, llm=self.llm)
        self.udp_analysis = udp_analysis or UdpAnalysisClient(self.settings)

    def warm_up(self) -> None:
        """Eagerly build the layer search index.

        Call once at application startup so the (relatively expensive)
        catalog fetch + embedding happens before the app accepts
        traffic, rather than blocking a user's first query.
        """

        self.layer_search.warm_up()

    def resolve_locations(self, query: str) -> list[LocationMatch]:
        # Hamburg's address index has no record for "Hamburg" the city
        # itself (it only indexes streets/districts/boroughs *within*
        # Hamburg), so a bare "Hamburg" query would otherwise resolve to
        # some arbitrary borough (e.g. "Hamburg-Harburg") - handle it
        # directly instead.
        if query.strip().lower() == "hamburg":
            return [_DEFAULT_LOCATION]

        match = self.address_resolver.resolve(query)
        return [match] if match else [_DEFAULT_LOCATION]

    def resolve_layer(
        self, query: str, allowed_layer_ids: Collection[str] | None = None
    ) -> LayerMatch | None:
        """:param allowed_layer_ids: when given, restricts resolution to
        these layer ids (``PortalCatalogResolver``, Section 8)."""

        search_terms = [query, *self._expand_query_terms(query)]

        best_by_id: dict[str, LayerMatch] = {}
        for term in search_terms:
            for candidate in self.layer_search.search(term, allowed_layer_ids=allowed_layer_ids):
                existing = best_by_id.get(candidate.layer_id)
                if existing is None or candidate.confidence > existing.confidence:
                    best_by_id[candidate.layer_id] = candidate

        if not best_by_id:
            return None

        candidates = self._dedupe_by_name(best_by_id.values())
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        candidates = candidates[: self.settings.layer_result_top_k]

        return self._select_layer_with_llm(query, candidates)

    @staticmethod
    def _dedupe_by_name(candidates) -> list[LayerMatch]:
        """Collapse candidates that share a name (e.g. the same dataset
        published as separate WMS/WFS/OAF/SensorThings documents) down
        to their highest-confidence instance, so the LLM disambiguation
        prompt doesn't waste slots on near-duplicate entries."""

        best_by_name: dict[str, LayerMatch] = {}
        for candidate in candidates:
            key = candidate.name.strip().lower()
            existing = best_by_name.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                best_by_name[key] = candidate
        return list(best_by_name.values())

    def _expand_query_terms(self, query: str) -> list[str]:
        """Ask Qwen3 for likely official term(s) matching ``query``.

        Best-effort: any failure (LLM unreachable, unparseable reply)
        just results in no expansion terms, falling back to searching
        the original query alone.
        """

        prompt = _QUERY_EXPANSION_PROMPT_TEMPLATE.format(query=query)

        try:
            reply = self.llm.generate(
                prompt, max_tokens=self.settings.layer_llm_max_tokens, label="layer_query_expansion"
            )
        except Exception:
            logger.warning("Layer query expansion LLM call failed", exc_info=True)
            return []

        terms = [term.strip() for term in reply.split(",")]
        return [term for term in terms if term]

    def _select_layer_with_llm(self, query: str, candidates: list[LayerMatch]) -> LayerMatch | None:
        if len(candidates) == 1:
            return candidates[0]

        prompt = _LAYER_SELECTION_PROMPT_TEMPLATE.format(
            query=query, candidates=_format_candidates(candidates)
        )

        try:
            reply = self.llm.generate(
                prompt, max_tokens=self.settings.layer_llm_max_tokens, label="layer_disambiguation"
            )
        except Exception:
            logger.warning(
                "Layer disambiguation LLM call failed, falling back to top retrieved candidate",
                exc_info=True,
            )
            return candidates[0]

        index = _parse_selection_index(reply, len(candidates))
        if index is None:
            logger.warning(
                "Could not parse layer disambiguation reply %r, falling back to top candidate",
                reply,
            )
            return candidates[0]
        if index == 0:
            logger.info("LLM determined no candidate layer matches query %r", query)
            return None

        return candidates[index - 1]

    def analyze(self, query: str, history: list[tuple[str, str]] | None = None) -> AnalysisResult:
        """An analytical question for the UDP agent (Section 13)."""

        return self.udp_analysis.analyze(query, history)

    def apply_filter(
        self,
        query: str,
        layer: LayerMatch | None,
        location: LocationMatch | None = None,
        mode: str = "replace",
        prior_conditions: list | None = None,
    ) -> FilterResult | None:
        """:param layer: the layer to filter - from this turn's
        layer_resolver call, or (a conversation-context follow-up
        naming an already-active layer) looked up in the portal's
        LayerCatalog instead.
        :param location: the already-resolved location (from this
        turn's location_extractor call, or re-resolved from the map
        state's lastLocation) - its boundary, when present, becomes a
        spatial predicate (see FilterAgent).
        :param mode: "refine" merges with prior_conditions (Section
        10.6); "replace" (default) starts from a clean slate.
        :param prior_conditions: the target layer's previously-applied
        conditions, from the map state's filters - only consulted when
        mode="refine"."""

        return self.filter_agent.filter(query, layer, location, mode=mode, prior_conditions=prior_conditions)

    def detect_action(self, query: str) -> ActionMatch:
        lowered = query.lower()

        for keywords, action_type in _ACTION_KEYWORDS:
            if any(keyword in lowered for keyword in keywords):
                return ActionMatch(action_type, 0.9)

        return ActionMatch(_DEFAULT_ACTION, 0.85)
