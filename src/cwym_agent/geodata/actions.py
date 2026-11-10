"""``action_detector``: which map action a query asks for, by keyword.

Plain keyword matching in German and English - no LLM, no service.
"""

import re

from cwym_agent.geodata.types import ActionMatch

# German and English, matched from the start of a word: "öffne" covers
# "öffnen" but not "geöffnete Apotheken", "filter" covers "filtere" but not
# "Luftfilter". "open" must be the whole word, so "opening hours" doesn't
# ask for a tool.
_ACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(navigier|fahre nach|navigate|go to|take me to|fly to|zoom to)"), "Navigieren"),
    (re.compile(r"\bfilter"), "Filter"),
    (re.compile(r"\b(öffne|oeffne)|\bopen\b"), "Öffne"),
]
_DEFAULT_ACTION = "Zeigen"


def detect_action_type(query: str) -> str:
    """The action label for ``query`` by keyword (German or English)."""

    lowered = query.lower()
    return next((action for pattern, action in _ACTION_PATTERNS if pattern.search(lowered)), _DEFAULT_ACTION)


def detect_action(query: str) -> ActionMatch:
    action_type = detect_action_type(query)
    return ActionMatch(action_type, 0.85 if action_type == _DEFAULT_ACTION else 0.9)
