"""The reply to a tier 1 analysis: fixed templates in German and English
(CHAT-11, cmym-agent-architecture.md 14.4 rule 4).

The numbers come from ``analysis.Computed`` and are only formatted here
(German decimal comma, at most two decimals) - no LLM rewords them, so
"105 von 235" can't become "rund 100". Every reply also says what was
left out: features without a value, unreadable values, ties beyond the
cut. A status other than "ok" is an honest sentence without a number.
"""

from cwym_agent.geodata.analysis import Bucket, Computed, parse_number
from cwym_agent.geodata.types import DatasetAnswer

_MAX_LISTED = 10

_T = {
    "de": {
        "matches": "Treffer",
        "objects": "Objekte",
        "in": " in {area}",
        "with": " mit {conditions}",
        "and": " und ",
        "contains": "enthält",
        "chart_title": "{attr}: Verteilung{where}",
        "missing_bucket": "keine Angabe",
        "other_bucket": "Sonstige",
        "count_layer": "„{layer}“: {n} Treffer{where}{cond}.",
        "count": "{n} {thing}{where}{cond}.",
        "distribution": "{attr} bei {n} {thing}{where}{cond}:",
        "top_number_desc": "Die {k} {thing} mit dem höchsten Wert für {attr}{where}{cond}:",
        "top_number_asc": "Die {k} {thing} mit dem niedrigsten Wert für {attr}{where}{cond}:",
        "top_ordinal_desc": "Die {k} {thing} mit {attr} am Ende der Reihenfolge {first}–{last}{where}{cond}:",
        "top_ordinal_asc": "Die {k} {thing} mit {attr} am Anfang der Reihenfolge {first}–{last}{where}{cond}:",
        "min": "Kleinster Wert für {attr}{where}{cond}: {value} ({k} {thing}: {items}).",
        "max": "Größter Wert für {attr}{where}{cond}: {value} ({k} {thing}: {items}).",
        "average": "Durchschnitt von {attr}{where}{cond}: {value} (aus {used} von {n} {thing}).",
        "sum": "Summe von {attr}{where}{cond}: {value} (aus {used} von {n} {thing}).",
        "no_values": "Für {attr} gibt es{where}{cond} keine Werte ({n} {thing}).",
        "lookup_found": "{key}: {k} {thing} gefunden.",
        "lookup_none": "Zu „{key}“ habe ich keine {thing} gefunden.",
        "lookup_nearby": " Vorhanden sind: {items}.",
        "note_missing": "{m} {thing} ohne Angabe zu {attr} sind nicht berücksichtigt.",
        "note_invalid": "{m} {thing} mit nicht auswertbarem Wert für {attr} sind nicht berücksichtigt.",
        "note_unknown": "Werte außerhalb der Reihenfolge: {values}.",
        "note_tie": "{m} weitere {thing} haben denselben Wert wie Platz {k}.",
        "note_shown": "Auf der Karte sind nur die ersten {k} hervorgehoben.",
        "more": "… und {m} weitere",
        "too_many": "Das sind zu viele {thing}{where} (mehr als {cap}), um sie vollständig auszuwerten – ich nenne deshalb keine Zahl. Grenze die Frage bitte weiter ein, z. B. auf ein Gebiet.",
        "service_error": "Die Daten konnten gerade nicht vollständig abgerufen werden – ich nenne deshalb keine Zahl. Bitte versuche es gleich noch einmal.",
        "needs_dataset": "Für „{layer}“ kann ich nur zählen. Verteilungen, Ranglisten, Durchschnitte und Suchen gehen nur für Datensätze, deren Merkmale im Stadtprofil beschrieben sind.",
        "not_filterable": "Die Ebene „{layer}“ liefert keine abfragbaren Daten, deshalb kann ich sie nicht auswerten.",
        "no_layer": "Ich weiß nicht, welche Daten ich auswerten soll. Nenne bitte die Kartenebene.",
        "no_attribute": "Ich weiß nicht, welches Merkmal ich auswerten soll. Möglich sind: {fields}.",
        "not_numeric": "{attr} ist kein Zahlenwert, deshalb kann ich keinen Durchschnitt und keine Summe bilden.",
        "not_sortable": "{attr} hat keine Reihenfolge, deshalb kann ich keine Rangliste und keinen kleinsten oder größten Wert bilden. Möglich ist eine Verteilung.",
        "bad_n": "Eine Rangliste kann ich mit 1 bis 20 Einträgen bilden.",
        "no_area": "Für „{place}“ habe ich keine Gebietsgrenze, deshalb kann ich dort nichts auswerten.",
        "no_lookup_field": "Für {thing} ist keine Suche nach Adresse oder Name eingerichtet.",
        "unknown_operation": "Diese Auswertung kann ich nicht durchführen.",
    },
    "en": {
        "matches": "matches",
        "objects": "features",
        "in": " in {area}",
        "with": " with {conditions}",
        "and": " and ",
        "contains": "contains",
        "chart_title": "{attr}: distribution{where}",
        "missing_bucket": "not specified",
        "other_bucket": "other",
        "count_layer": "“{layer}”: {n} matches{where}{cond}.",
        "count": "{n} {thing}{where}{cond}.",
        "distribution": "{attr} for {n} {thing}{where}{cond}:",
        "top_number_desc": "The {k} {thing} with the highest {attr}{where}{cond}:",
        "top_number_asc": "The {k} {thing} with the lowest {attr}{where}{cond}:",
        "top_ordinal_desc": "The {k} {thing} with {attr} at the end of the scale {first}–{last}{where}{cond}:",
        "top_ordinal_asc": "The {k} {thing} with {attr} at the start of the scale {first}–{last}{where}{cond}:",
        "min": "Lowest {attr}{where}{cond}: {value} ({k} {thing}: {items}).",
        "max": "Highest {attr}{where}{cond}: {value} ({k} {thing}: {items}).",
        "average": "Average {attr}{where}{cond}: {value} (from {used} of {n} {thing}).",
        "sum": "Sum of {attr}{where}{cond}: {value} (from {used} of {n} {thing}).",
        "no_values": "There are no values for {attr}{where}{cond} ({n} {thing}).",
        "lookup_found": "{key}: {k} {thing} found.",
        "lookup_none": "I found no {thing} at “{key}”.",
        "lookup_nearby": " There are: {items}.",
        "note_missing": "{m} {thing} without a value for {attr} are not included.",
        "note_invalid": "{m} {thing} with an unreadable value for {attr} are not included.",
        "note_unknown": "Values outside the scale: {values}.",
        "note_tie": "{m} more {thing} share the value of place {k}.",
        "note_shown": "Only the first {k} are highlighted on the map.",
        "more": "… and {m} more",
        "too_many": "There are too many {thing}{where} (more than {cap}) to analyse them completely, so I won't give a number. Please narrow the question down, e.g. to an area.",
        "service_error": "The data couldn't be fetched completely just now, so I won't give a number. Please try again in a moment.",
        "needs_dataset": "For “{layer}” I can only count. Distributions, rankings, averages and lookups only work for datasets whose attributes are described in the city profile.",
        "not_filterable": "The layer “{layer}” has no queryable data, so I can't analyse it.",
        "no_layer": "I don't know which data to analyse. Please name the map layer.",
        "no_attribute": "I don't know which attribute to analyse. Possible: {fields}.",
        "not_numeric": "{attr} is not a number, so I can't compute an average or a sum.",
        "not_sortable": "{attr} has no order, so I can't rank it or give a lowest or highest value. A distribution is possible.",
        "bad_n": "I can rank 1 to 20 entries.",
        "no_area": "I have no boundary for “{place}”, so I can't analyse anything there.",
        "no_lookup_field": "Looking up {thing} by address or name is not set up.",
        "unknown_operation": "I can't do this analysis.",
    },
}

_OPERATOR_SIGNS = {"eq": "=", "ne": "≠", "lt": "<", "lte": "≤", "gt": ">", "gte": "≥"}


def format_number(value, language: str) -> str:
    """1952 -> "1952", 57.25 -> "57,25" (de) / "57.25" (en); at most two
    decimals, no thousands separators (years must stay "1952")."""

    if isinstance(value, str):
        return value
    number = round(float(value), 2)
    text = str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if language == "de" else text


def chart(answer: DatasetAnswer, language: str) -> dict | None:
    """The SHOW_CHART payload for a distribution (camelCase wire names),
    in ``language``; None for anything else. Same numbers as the text."""

    if answer.status != "ok" or answer.operation != "distribution" or not answer.computed.buckets:
        return None
    t, ctx = _T[language], _Context(answer, language)
    base = ctx.base()
    data = []
    for bucket in answer.computed.buckets:
        label = t["missing_bucket"] if bucket.kind == "missing" else t["other_bucket"] if bucket.kind == "other" else bucket.label
        data.append({"label": label, "value": bucket.count, "color": bucket.color, "kind": bucket.kind})
    return {
        "title": t["chart_title"].format(attr=base["attr"], where=f" ({answer.area_name})" if answer.area_name else ""),
        "chartType": "bar",
        "data": data,
        "total": answer.computed.total,
        "datasetKey": answer.dataset_key,
        "attribute": answer.attribute,
    }


def render(answer: DatasetAnswer, language: str) -> str:
    t = _T[language]
    ctx = _Context(answer, language)
    if answer.status != "ok":
        return t.get(answer.status, t["service_error"]).format(**ctx.base())

    computed: Computed = answer.computed
    operation = answer.operation
    if operation == "count":
        key = "count" if answer.dataset_label else "count_layer"
        return t[key].format(**ctx.base(), n=computed.total)
    if operation == "distribution":
        head = t["distribution"].format(**ctx.base(), n=computed.total)
        return _block(head, _bucket_lines(computed.buckets, t), ctx.notes(computed, missing=False))
    if operation == "top":
        return _top(ctx, computed)
    if operation in ("min", "max"):
        if computed.value is None:
            return ctx.with_notes(t["no_values"].format(**ctx.base(), n=computed.total), computed)
        items = ctx.items(computed.rows)
        text = t[operation].format(**ctx.base(), value=ctx.value(computed.value), k=len(computed.rows), items=items)
        return ctx.with_notes(text, computed)
    if operation in ("average", "sum"):
        if computed.value is None:
            return ctx.with_notes(t["no_values"].format(**ctx.base(), n=computed.total), computed)
        text = t[operation].format(**ctx.base(), value=ctx.value(computed.value), used=computed.used, n=computed.total)
        return ctx.with_notes(text, computed)
    if operation == "lookup":
        return _lookup(ctx, computed)
    return t["unknown_operation"]


class _Context:
    def __init__(self, answer: DatasetAnswer, language: str):
        self.answer, self.language, self.t = answer, language, _T[language]

    def label(self, field: str | None) -> str:
        if field is None:
            return ""
        labels = self.answer.labels.get(field) or {}
        return labels.get(self.language) or labels.get("de") or field

    def thing(self) -> str:
        label = self.answer.dataset_label
        if label:
            return label.get(self.language) or next(iter(label.values()))
        return self.t["objects"]

    def value(self, value) -> str:
        return format_number(value, self.language)

    def conditions(self) -> str:
        parts = []
        for c in self.answer.conditions:
            sign = self.t["contains"] if c.operator == "contains" else _OPERATOR_SIGNS.get(c.operator, c.operator)
            parts.append(f"{self.label(c.field)} {sign} {self.condition_value(c.value)}")
        return self.t["with"].format(conditions=self.t["and"].join(parts)) if parts else ""

    def condition_value(self, value) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return self.value(value)
        return str(value)

    def base(self) -> dict:
        a = self.answer
        order = a.order or []
        return {
            "thing": self.thing(),
            "attr": self.label(a.attribute),
            "where": self.t["in"].format(area=a.area_name) if a.area_name else "",
            "cond": self.conditions(),
            "layer": a.layer_name or "",
            "cap": a.cap,
            "place": a.place or "",
            "fields": ", ".join(self.label(f) for f in a.show_fields),
            "first": order[0] if order else "",
            "last": order[-1] if order else "",
        }

    def name(self, feature: dict) -> str:
        properties = feature.get("properties") or {}
        return str(properties.get(self.answer.item_field) or feature.get("id") or "?")

    def items(self, rows: list[dict]) -> str:
        names = [self.name(r) for r in rows[:_MAX_LISTED]]
        if len(rows) > _MAX_LISTED:
            names.append(self.t["more"].format(m=len(rows) - _MAX_LISTED))
        return ", ".join(names)

    def notes(self, computed: Computed, missing: bool = True) -> list[str]:
        """What was left out. ``missing=False`` for a distribution, which
        shows missing values as their own line."""

        base = self.base()
        notes = []
        if computed.missing and missing:
            notes.append(self.t["note_missing"].format(**base, m=computed.missing))
        if computed.invalid:
            notes.append(self.t["note_invalid"].format(**base, m=computed.invalid))
        if computed.unknown_values:
            notes.append(self.t["note_unknown"].format(values=", ".join(computed.unknown_values)))
        return notes

    def with_notes(self, text: str, computed: Computed) -> str:
        return " ".join([text, *self.notes(computed)])


def _block(head: str, lines: list[str], notes: list[str]) -> str:
    text = head + "\n" + "\n".join(lines)
    return f"{text}\n\n{' '.join(notes)}" if notes else text


def _bucket_lines(buckets: list[Bucket], t: dict) -> list[str]:
    lines = []
    for bucket in buckets:
        label = t["missing_bucket"] if bucket.kind == "missing" else t["other_bucket"] if bucket.kind == "other" else bucket.label
        lines.append(f"- {label}: {bucket.count}")
    return lines


def _top(ctx: _Context, computed: Computed) -> str:
    answer, t = ctx.answer, ctx.t
    if not computed.rows:
        return ctx.with_notes(t["no_values"].format(**ctx.base(), n=computed.total), computed)
    direction = "desc" if computed.descending else "asc"
    kind = "ordinal" if answer.attribute_type == "ordinal" else "number"
    head = t[f"top_{kind}_{direction}"].format(**ctx.base(), k=len(computed.rows))
    lines = [
        f"{i}. {ctx.name(row)}: {ctx.value(_raw(row, answer.attribute))}"
        for i, row in enumerate(computed.rows, start=1)
    ]
    notes = ctx.notes(computed)
    if computed.tie_beyond:
        notes.append(t["note_tie"].format(**ctx.base(), m=computed.tie_beyond, k=len(computed.rows)))
    return _block(head, lines, notes)


def _raw(feature: dict, field: str | None):
    """A property for display: numbers as numbers (formatted later)."""

    raw = (feature.get("properties") or {}).get(field)
    number = parse_number(raw)
    return number if number is not None else raw


def _lookup(ctx: _Context, computed: Computed) -> str:
    t = ctx.t
    key = computed.lookup_key or ""
    if not computed.rows:
        text = t["lookup_none"].format(**ctx.base(), key=key)
        if computed.nearby:
            text += t["lookup_nearby"].format(items=", ".join(computed.nearby[:_MAX_LISTED]))
        return text
    lines = []
    for row in computed.rows[:_MAX_LISTED]:
        properties = row.get("properties") or {}
        details = "; ".join(
            f"{ctx.label(field)}: {ctx.value(_raw(row, field))}"
            for field in ctx.answer.show_fields
            if properties.get(field) not in (None, "")
        )
        lines.append(f"- {ctx.name(row)}: {details}")
    if len(computed.rows) > _MAX_LISTED:
        lines.append(t["more"].format(m=len(computed.rows) - _MAX_LISTED))
    return t["lookup_found"].format(**ctx.base(), key=key, k=len(computed.rows)) + "\n" + "\n".join(lines)
