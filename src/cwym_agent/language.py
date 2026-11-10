"""The answer language of a turn (CHAT-7, cmym-agent-architecture.md 14.3).

The data CWYM works with is German (layer names, attribute values, place
names), and so are its internal prompts. The answer, however, is in the
user's language: German or English, if the city profile offers it.

``detect_language`` guesses the language of one message from function
words. It is ported from ASCEND's ``app/chat/language.py`` with its known
flaws fixed:

- Words that are common in both languages, or in Romance languages, are
  in neither list ("an", "also", "die", "in", "was", "a", "no", "me",
  "on", "do", "du", "es", "i", "zoom" ...): "An der Hasenleite 5" is
  German, and Italian, Spanish or French must not come out as English or
  German.
- Umlauts/ß only break a tie between counted words. Alone they say
  nothing (a German place name in an English question is common), and
  they never tie a clear English question ("Who owns Münchner Wohnen?").
- Inconclusive is a result: "ok", "Altona?", "27764" have no language.

``resolve_language`` turns that into a decision for the turn, in this
order: the message itself, the user's earlier messages (an "ok" after an
English conversation stays English), the portal's UI language, the
profile's default language. Only the profile's languages count at every
step.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

_WORD_RE = re.compile(r"[a-zà-öø-ÿäöüß]+", re.IGNORECASE)
_UMLAUT_RE = re.compile(r"[äöüß]", re.IGNORECASE)
# "für"/"fuer", "Gebäude"/"Gebaeude" hit the same marker.
_UMLAUT_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

_GERMAN_MARKERS = frozenset({
    "der", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "eines",
    "und", "oder", "aber", "nicht", "kein", "keine", "nur", "auch", "noch", "schon", "sehr",
    "wie", "wieviel", "wieviele", "viele", "welche", "welcher", "welches", "wo", "wer",
    "warum", "wann", "woher", "wohin",
    "ist", "sind", "hat", "haben", "wird", "werden", "gibt", "kann", "kannst", "koennen", "moechte",
    "mit", "von", "vom", "fuer", "auf", "aus", "bei", "zu", "zum", "zur", "nach",
    "ueber", "unter", "zwischen", "durch", "gegen", "ohne", "im", "ins",
    "mir", "mich", "dir", "dich", "ich", "wir", "uns", "ihr", "sie",
    "zeig", "zeige", "zeigen", "anzeigen", "gib", "sag", "sage", "finde", "suche",
    "oeffne", "blende", "filtere", "entferne",
    "karte", "ebene", "ebenen", "alle", "allen", "jede", "jeder",
    "bitte", "danke", "hallo", "hier", "dort", "gerne", "ja", "nein",
    # Map vocabulary, for short commands without a function word
    # ("Filtere Krankenhäuser in Wandsbek").
    "krankenhaus", "krankenhaeuser", "schule", "schulen", "grundschulen", "kita", "kitas",
    "spielplatz", "spielplaetze", "apotheke", "apotheken", "haltestelle", "haltestellen",
    "fahrradstationen", "gebaeude", "strasse", "bezirk", "stadtteil", "gebiet",
})

_ENGLISH_MARKERS = frozenset({
    "the", "and", "or", "but", "not", "only", "too", "still", "very",
    "how", "many", "much", "which", "what", "where", "who", "why", "when",
    "is", "are", "were", "have", "does", "did", "can", "could", "would", "should",
    "with", "of", "for", "from", "at", "to", "into", "about", "between", "through",
    "without", "near", "around", "inside", "within",
    "my", "you", "your", "we", "us", "they", "them", "it", "its",
    "show", "give", "tell", "find", "display", "open", "remove", "hide", "please",
    "map", "all", "each", "every", "any", "there", "here",
    "thanks", "thank", "hello", "hi", "yes",
    # Map vocabulary, for short commands without a function word
    # ("Filter hospitals in Wandsbek"). Not "park(s)", "bus", "station":
    # German uses them too.
    "hospital", "hospitals", "school", "schools", "kindergartens", "playground", "playgrounds",
    "pharmacy", "pharmacies", "stops", "bike", "bikes", "buildings", "street", "district",
    "districts", "area", "areas",
})

Language = Literal["de", "en"]
LanguageSource = Literal["detected", "history", "ui", "default"]


@dataclass(frozen=True)
class LanguageNames:
    """How the (German) prompts name a language."""

    adjective: str
    """German adjective, inflected for "die ... Nutzeranfrage/Antwort"."""
    german: str
    """German name, "auf ..."."""
    english: str
    """English name, for directives written in English."""


LANGUAGE_NAMES: dict[str, LanguageNames] = {
    "de": LanguageNames(adjective="deutsche", german="Deutsch", english="German"),
    "en": LanguageNames(adjective="englische", german="Englisch", english="English"),
}
SUPPORTED_LANGUAGES = tuple(LANGUAGE_NAMES)
"""The answer languages CWYM can detect and answer in."""


def detect_language(text: str) -> Language | None:
    """"de", "en", or None when the message gives no clear evidence."""

    if not text:
        return None
    lowered = text.lower()
    tokens = {word.translate(_UMLAUT_MAP) for word in _WORD_RE.findall(lowered)}

    german = len(tokens & _GERMAN_MARKERS)
    english = len(tokens & _ENGLISH_MARKERS)
    if german == english and german > 0 and _UMLAUT_RE.search(lowered):
        german += 1

    if german > english:
        return "de"
    if english > german:
        return "en"
    return None


def normalize_language(code: str | None) -> str | None:
    """"en-US" / "EN" -> "en"; anything that isn't a language tag -> None."""

    if not code:
        return None
    primary = code.strip().lower().replace("_", "-").split("-")[0]
    return primary if len(primary) == 2 and primary.isalpha() else None


@dataclass(frozen=True)
class TurnLanguage:
    code: str
    """ISO 639-1, one of the profile's languages."""
    source: LanguageSource
    """What decided it - recorded in the run trace."""


def resolve_language(
    query: str,
    history: Sequence[tuple[str, str]],
    ui_language: str | None,
    supported: Sequence[str],
) -> TurnLanguage:
    """The answer language for this turn.

    :param query: the user's message as typed (not a query rebuilt by the
        clarification resolver).
    :param history: earlier (role, text) turns, oldest first.
    :param ui_language: the portal's current UI language, if the client
        sent one.
    :param supported: the profile's languages; the first is the default.
    """

    detected = detect_language(query)
    if detected in supported:
        return TurnLanguage(detected, "detected")

    for role, text in reversed(history):
        if role != "user":
            continue
        earlier = detect_language(text)
        if earlier in supported:
            return TurnLanguage(earlier, "history")

    ui = normalize_language(ui_language)
    if ui in supported:
        return TurnLanguage(ui, "ui")

    return TurnLanguage(supported[0], "default")
