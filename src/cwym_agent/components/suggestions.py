"""Follow-up suggestions at the end of a reply (CHAT-14, architecture 14.5).

The reply LLM ends its answer with a block

    VORSCHLÄGE:
    - Zeige Schulen in Altona
    - Wie viele Kitas gibt es dort?

which must not reach the user as text: ``SuggestionSplitter`` passes the
streamed answer through but holds back the last few characters until it
is sure they don't start the marker (which may arrive split across
chunks), and from the marker on keeps everything for itself. The items
then go out as a ``SHOW_SUGGESTIONS`` command.
"""

import re

MAX_SUGGESTIONS = 3
MAX_LENGTH = 120

# The prompt asks for "VORSCHLÄGE:"; an English answer may translate it.
_MARKER = re.compile(r"(?:VORSCHL(?:Ä|AE)GE|SUGGESTIONS)\s*:", re.IGNORECASE)
_HOLD_BACK = len("VORSCHLAEGE :") + 2
_ITEM_PREFIX = re.compile(r"^\s*(?:[-*•–]|\d+[.)])\s*")

INSTRUCTION = {
    "de": (
        "\n\nHänge an die Antwort eine Zeile 'VORSCHLÄGE:' an und darunter bis "
        "zu 3 kurze Folgefragen, die der Nutzer als Nächstes stellen könnte - je "
        "eine pro Zeile, mit '- ' davor, als direkte Aufforderung in der Sprache "
        "der Antwort (z. B. '- Zeige Schulen in der Nähe'), höchstens 8 Wörter. "
        "Nur Fragen, die mit der Karte beantwortbar sind."
    ),
}
"""Appended to the (German) reply prompt, before any language directive."""


class SuggestionSplitter:
    def __init__(self) -> None:
        self._buffer = ""
        self._emitted = 0
        self._marker_at: int | None = None

    def feed(self, chunk: str) -> str:
        """The part of the answer that is safe to show now."""

        self._buffer += chunk
        if self._marker_at is not None:
            return ""
        match = _MARKER.search(self._buffer, self._emitted)
        if match is not None:
            self._marker_at = match.start()
            return self._take(self._marker_at)
        return self._take(max(self._emitted, len(self._buffer) - _HOLD_BACK))

    def finish(self) -> tuple[str, list[str]]:
        """(the rest of the answer, the suggestions)."""

        if self._marker_at is None:
            match = _MARKER.search(self._buffer, self._emitted)
            if match is not None:
                self._marker_at = match.start()
        if self._marker_at is None:
            return self._take(len(self._buffer)), []
        rest = self._take(self._marker_at)
        block = self._buffer[self._marker_at :]
        return rest, parse_items(_MARKER.sub("", block, count=1))

    def answer(self) -> str:
        """The answer without the block (after ``finish``)."""

        end = self._marker_at if self._marker_at is not None else len(self._buffer)
        return self._buffer[:end].rstrip()

    def _take(self, end: int) -> str:
        text = self._buffer[self._emitted : end]
        self._emitted = max(self._emitted, end)
        return text


def parse_items(block: str) -> list[str]:
    items = []
    for line in block.splitlines():
        text = _ITEM_PREFIX.sub("", line).strip().strip("\"'„“”").strip()
        if text and len(text) <= MAX_LENGTH and text not in items:
            items.append(text)
        if len(items) == MAX_SUGGESTIONS:
            break
    return items


def split(text: str) -> tuple[str, list[str]]:
    """(answer, suggestions) for a complete reply."""

    splitter = SuggestionSplitter()
    splitter.feed(text)
    _, items = splitter.finish()
    return splitter.answer(), items
