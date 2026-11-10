"""Tier 1 analysis of one dataset (CHAT-11, cmym-agent-architecture.md
14.4): *one dataset -> optional filters (attributes and/or an area) ->
one fixed operation on one attribute.*

Part of the ``filter_agent`` sub-agent: the planner asks for an
operation; this module reuses ``FilterAgent``'s HTTP clients, caches and
LLM. The LLM only translates the question into conditions, the attribute
and N; everything that produces a number is code (``analysis``,
``analysis_data``), and the reply states it through fixed templates
(``analysis_text``).

Which data:

- a layer that shows a declared dataset (``CityProfile.datasets``) ->
  that dataset, with typed attributes: every operation;
- no layer and exactly one declared dataset -> that dataset;
- any other filterable layer (WFS/OAF) -> only ``count`` (no types
  needed); other operations get an honest "needs a declared dataset".
"""

import json
import logging
import re
from dataclasses import dataclass, replace

from cwym_agent.city_profile import AttributeConfig, DatasetConfig, FileSourceConfig, OafSourceConfig, WfsSourceConfig
from cwym_agent.geodata import analysis
from cwym_agent.geodata.analysis import NEEDS_ATTRIBUTE, AnalysisError
from cwym_agent.geodata.analysis_data import (
    AnalysisSource,
    FileSource,
    OafSource,
    WfsSource,
    load_feature_collection,
)
from cwym_agent.geodata.filter_agent import FILTER_OPERATORS, FilterAgent
from cwym_agent.geodata.types import DatasetAnswer, FilterCondition, LayerMatch, LocationMatch

logger = logging.getLogger(__name__)

_MAX_PROMPT_VALUES = 12

_ANALYSIS_EXTRACTION_PROMPT_TEMPLATE = (
    "Du hilfst dabei, eine Auswertungsfrage zu einem Datensatz in "
    "strukturierte Angaben zu übersetzen. Gewünschte Auswertung: {operation}. "
    "Felder des Datensatzes:\n{fields}\n\n"
    "Anfrage: \"{query}\"\n\n"
    "Antworte NUR mit einem JSON-Objekt der Form {{\"conditions\": [<Bedingungen der Form "
    "{{\"field\": <Feldname>, \"operator\": <eq|ne|lt|lte|gt|gte|contains>, \"value\": <Wert>}}>], "
    "\"attribute\": <Feldname, der ausgewertet wird, oder null>, "
    "\"n\": <gewünschte Anzahl bei einer Rangliste, oder null>, "
    "\"order\": <\"desc\" für die höchsten/größten/neuesten Werte bzw. das Ende der Reihenfolge, "
    "\"asc\" für die niedrigsten/kleinsten/ältesten Werte bzw. den Anfang der Reihenfolge, oder null>, "
    "\"lookup\": <gesuchte Adresse oder gesuchter Name, oder null>}}. "
    "Verwende NUR Felder aus der obigen Liste. Sind Werte angegeben, verwende den passenden "
    "Wert genau so geschrieben. Bei Feldern mit Reihenfolge vergleichen lt/gt nach dieser "
    "Reihenfolge. Eine Bedingung auf das ausgewertete Feld nur dann, wenn die Anfrage es "
    "ausdrücklich einschränkt. Ein Ortsname ({area_terms}) ist "
    "KEINE Bedingung - die räumliche Einschränkung erfolgt separat."
)

_OPERATION_NAMES = {
    "count": "Anzahl",
    "distribution": "Verteilung der Werte eines Feldes",
    "top": "Rangliste (die N Objekte mit den höchsten oder niedrigsten Werten)",
    "min": "kleinster Wert eines Feldes",
    "max": "größter Wert eines Feldes",
    "average": "Durchschnitt eines Zahlenfeldes",
    "sum": "Summe eines Zahlenfeldes",
    "lookup": "ein Objekt nach Adresse oder Name finden",
}

_TYPE_NAMES = {"number": "Zahl", "category": "Kategorie", "ordinal": "Rangfolge"}


@dataclass
class LoadedDataset:
    key: str
    config: DatasetConfig
    features: list[dict] | None = None
    """A file dataset's features, loaded at startup."""


def load_datasets(datasets: dict[str, DatasetConfig]) -> dict[str, LoadedDataset]:
    """Loads the file datasets (raises ``DatasetError`` with the path at
    startup rather than at the first question)."""

    loaded = {}
    for key, config in datasets.items():
        features = load_feature_collection(config.source.path) if isinstance(config.source, FileSourceConfig) else None
        if features is not None:
            logger.info("Dataset %s: %d features from %s", key, len(features), config.source.path)
        loaded[key] = LoadedDataset(key, config, features)
    return loaded


@dataclass
class _Spec:
    conditions: list[FilterCondition]
    attribute: str | None = None
    n: int | None = None
    order: str | None = None
    lookup: str | None = None


class Tier1Analyzer:
    def __init__(self, filter_agent: FilterAgent, datasets: dict[str, LoadedDataset], city_name: str):
        self.filter_agent = filter_agent
        self.datasets = datasets
        self.city_name = city_name

    @property
    def settings(self):
        return self.filter_agent.settings

    def analyze(
        self, query: str, operation: str, layer: LayerMatch | None, location: LocationMatch | None = None
    ) -> DatasetAnswer:
        if operation not in analysis.OPERATIONS:
            raise ValueError(f"unknown tier 1 operation {operation!r}")
        answer = DatasetAnswer(operation=operation, status="ok", cap=self.settings.analysis_max_features)
        if layer is not None:
            answer.layer_id, answer.layer_name = layer.layer_id, layer.name

        boundary = None
        if location is not None:
            if location.boundary:
                boundary = location.boundary
                answer.area_name, answer.area_kind = location.name, location.kind
            elif location.name.strip().lower() != self.city_name.strip().lower():
                # Counting the whole city for "in der Mönckebergstraße"
                # would be a wrong answer to the question asked.
                return replace(answer, status="no_area", place=location.name)

        dataset = self._dataset_for(layer)
        if dataset is not None:
            return self._analyze_dataset(query, operation, dataset, boundary, answer)
        if layer is None:
            return replace(answer, status="no_layer")
        if operation != "count":
            return replace(answer, status="needs_dataset")
        return self._count_layer(query, layer, boundary, answer)

    # --- which data ------------------------------------------------------------

    def _dataset_for(self, layer: LayerMatch | None) -> LoadedDataset | None:
        if layer is not None:
            return next((d for d in self.datasets.values() if layer.layer_id in d.config.layer_ids), None)
        return next(iter(self.datasets.values())) if len(self.datasets) == 1 else None

    def _dataset_source(self, dataset: LoadedDataset) -> tuple[AnalysisSource | None, dict[str, list[str]]]:
        """The dataset's source and known values per field (for the prompt)."""

        config, attributes = dataset.config, dataset.config.attributes
        fa = self.filter_agent
        source = config.source
        if isinstance(source, FileSourceConfig):
            return FileSource(dataset.features or [], attributes), _values(dataset.features or [], attributes)
        if isinstance(source, OafSourceConfig):
            queryables = fa.queryables_cache.get(source.url.rstrip("/"), source.collection)
            if queryables is None:
                return None, {}
            geometry = fa.queryables_cache.geometry_field(queryables)
            return OafSource(source.url, source.collection, queryables, geometry, attributes, fa._client, fa.settings), {}
        assert isinstance(source, WfsSourceConfig)
        schema = fa.wfs.schema(source.url.split("?")[0], source.feature_type)
        if schema is None:
            return None, {}
        return WfsSource(source.url, source.feature_type, schema, attributes, fa.wfs, fa.settings), schema.examples

    # --- a declared dataset -------------------------------------------------------

    def _analyze_dataset(self, query, operation, dataset: LoadedDataset, boundary, answer: DatasetAnswer) -> DatasetAnswer:
        config = dataset.config
        answer.dataset_label = dict(config.label)
        answer.labels = {name: dict(a.label) for name, a in config.attributes.items()}
        answer.item_field = config.lookup_field or config.id_field
        answer.show_fields = list(config.attributes)
        answer.layer_id = answer.layer_id or (config.layers[0] if config.layers else None)

        source, values = self._dataset_source(dataset)
        if source is None:
            return replace(answer, status="service_error")
        if operation == "lookup" and not config.lookup_field:
            return replace(answer, status="no_lookup_field")

        spec = self._extract_spec(query, operation, config, values)
        answer.conditions = spec.conditions
        attribute = config.attributes.get(spec.attribute) if spec.attribute else None
        if operation in NEEDS_ATTRIBUTE:
            if attribute is None:
                return replace(answer, status="no_attribute")
            answer.attribute, answer.attribute_type, answer.order = spec.attribute, attribute.type, attribute.order
            answer.display_layer = attribute.display_layer if operation == "distribution" else None

        conditions = [] if operation == "lookup" else spec.conditions
        data = source.fetch(conditions, boundary, need_features=True)
        if not data.complete:
            return replace(answer, status=data.reason or "service_error")
        features = data.features or []

        try:
            if operation == "lookup":
                computed = analysis.lookup(features, config.lookup_field, spec.lookup or query)
            else:
                computed = analysis.compute(
                    operation, features, spec.attribute, attribute,
                    n=spec.n or 5, descending=spec.order != "asc",
                )
        except AnalysisError as error:
            return replace(answer, status=error.reason)

        shown = features if operation == "count" else computed.rows
        return self._with_features(replace(answer, computed=computed), shown)

    def _extract_spec(self, query, operation, config: DatasetConfig, values: dict[str, list[str]]) -> _Spec:
        fields = "\n".join(_field_line(name, a, values.get(name)) for name, a in config.attributes.items())
        if config.lookup_field and config.lookup_field not in config.attributes:
            fields += f"\n- {config.lookup_field} (Text) - Adresse/Name"
        prompt = _ANALYSIS_EXTRACTION_PROMPT_TEMPLATE.format(
            operation=_OPERATION_NAMES[operation], fields=fields, query=query,
            area_terms=self.filter_agent.area_terms,
        )
        try:
            reply = self.filter_agent.llm.generate(
                prompt, max_tokens=self.settings.filter_llm_max_tokens, label="analysis_extraction"
            )
        except Exception:
            logger.warning("tier 1: analysis extraction LLM call failed", exc_info=True)
            return _Spec([])
        known = set(config.attributes) | {config.lookup_field} - {None}
        return _parse_spec(reply, known)

    # --- a plain layer: count only ---------------------------------------------------

    def _count_layer(self, query, layer: LayerMatch, boundary, answer: DatasetAnswer) -> DatasetAnswer:
        fa = self.filter_agent
        if layer.typ == "WFS" and layer.url and layer.feature_type:
            url = layer.url.split("?")[0]
            schema = fa.wfs.schema(url, layer.feature_type)
            if schema is None:
                return replace(answer, status="service_error")
            queryables = {name: {"type": t} for name, t in schema.fields.items()}
            conditions = fa._extract_conditions(query, queryables, schema.examples)
            conditions += fa._conditions_from_layer_name(layer, schema.examples, conditions)
            source: AnalysisSource = WfsSource(url, layer.feature_type, schema, {}, fa.wfs, fa.settings)
        elif layer.typ == "OAF" and layer.url and layer.collection:
            api_url = layer.url.rstrip("/")
            queryables = fa.queryables_cache.get(api_url, layer.collection)
            if queryables is None:
                return replace(answer, status="service_error")
            conditions = fa._extract_conditions(query, queryables)
            geometry = fa.queryables_cache.geometry_field(queryables)
            source = OafSource(api_url, layer.collection, queryables, geometry, {}, fa._client, fa.settings)
        else:
            return replace(answer, status="not_filterable")

        answer.conditions = conditions
        data = source.fetch(conditions, boundary, need_features=True)
        if not data.complete and data.reason == "too_many" and boundary is None:
            # Too many to show or check one by one, but the server's own
            # total is exact without an area.
            data = source.fetch(conditions, None, need_features=False)
        if not data.complete or data.total is None:
            return replace(answer, status=data.reason or "service_error")
        return self._with_features(replace(answer, computed=analysis.Computed("count", total=data.total)), data.features or [])

    def _with_features(self, answer: DatasetAnswer, features: list[dict]) -> DatasetAnswer:
        limit = self.settings.filter_result_limit
        return replace(answer, features=features[:limit], features_truncated=len(features) > limit)


def _values(features: list[dict], attributes: dict[str, AttributeConfig]) -> dict[str, list[str]]:
    """Known values of category attributes (a few, for the prompt)."""

    values: dict[str, list[str]] = {}
    for name, attribute in attributes.items():
        if attribute.type != "category":
            continue
        found = sorted({str(p[name]).strip() for f in features if (p := f.get("properties") or {}).get(name) not in (None, "")})
        if 0 < len(found) <= _MAX_PROMPT_VALUES:
            values[name] = found
    return values


def _field_line(name: str, attribute: AttributeConfig, values: list[str] | None) -> str:
    line = f"- {name} ({_TYPE_NAMES[attribute.type]}) - {attribute.label.get('de') or next(iter(attribute.label.values()))}"
    if attribute.type == "ordinal":
        line += f" - Reihenfolge: {', '.join(attribute.order or [])}"
    elif values:
        line += f" - Werte: {', '.join(values)}"
    return line


def _parse_spec(reply: str, known_fields: set[str]) -> _Spec:
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    try:
        data = json.loads(match.group()) if match else {}
    except json.JSONDecodeError:
        logger.warning("tier 1: could not parse analysis extraction reply %r", reply)
        data = {}
    if not isinstance(data, dict):
        data = {}
    conditions = []
    for item in data.get("conditions") or []:
        if (
            isinstance(item, dict)
            and item.get("field") in known_fields
            and item.get("operator") in FILTER_OPERATORS
            and item.get("value") is not None
        ):
            conditions.append(FilterCondition(item["field"], item["operator"], item["value"]))
    attribute = data.get("attribute") if data.get("attribute") in known_fields else None
    n = data.get("n")
    n = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 1 else None
    order = data.get("order") if data.get("order") in ("asc", "desc") else None
    lookup = data.get("lookup") if isinstance(data.get("lookup"), str) and data["lookup"].strip() else None
    return _Spec(conditions, attribute, min(n, analysis.MAX_TOP) if n else None, order, lookup)
