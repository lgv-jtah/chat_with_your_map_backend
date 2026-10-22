"""Asking back for an ambiguous place, and understanding the answer.

"Grundschulen in Hoheluft" - Hamburg has Hoheluft-Ost and Hoheluft-West,
so the assistant asks which one is meant (``question``). The user then
usually answers with just the place ("Hoheluft-West", "West"). Found live:
handing such a two-word reply to the planner was unreliable - it rebuilt
the original request from the chat history in only 2 of 5 runs (others
switched on the unfiltered layer or did nothing). ``resolve_reply``
therefore turns the reply back into the complete original question with
the chosen place ("Grundschulen in Hoheluft-West"), deterministically,
before planning.
"""

import os
import re
from collections.abc import Sequence

_QUESTION_PREFIX = "Den Ort gibt es mehrfach – meinst du "
_QUESTION_SUFFIX = "? Die Karte habe ich noch nicht verändert."
_QUESTION_PATTERN = re.compile(re.escape(_QUESTION_PREFIX) + r"(.+?)" + re.escape(_QUESTION_SUFFIX))


def question(names: Sequence[str]) -> str:
    """The question back, e.g. "Den Ort gibt es mehrfach – meinst du
    Hoheluft-Ost oder Hoheluft-West? Die Karte habe ich noch nicht
    verändert." - fixed text (not LLM-written), so ``resolve_reply`` can
    read the offered places back from the chat history."""

    options = ", ".join(names[:-1]) + f" oder {names[-1]}" if len(names) > 1 else names[0]
    return f"{_QUESTION_PREFIX}{options}{_QUESTION_SUFFIX}"


def _offered_names(answer: str) -> list[str]:
    match = _QUESTION_PATTERN.search(answer)
    if match is None:
        return []
    head, _, last = match.group(1).rpartition(" oder ")
    return [name.strip() for name in head.split(", ") if name.strip()] + [last.strip()]


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _chosen(reply: str, names: list[str]) -> str | None:
    """The offered place the reply picks: its full name ("Hoheluft-West")
    or the part that tells the options apart ("West")."""

    reply_lower = reply.lower()
    full = [name for name in names if name.lower() in reply_lower]
    if len(full) == 1:
        return full[0]

    reply_words = _words(reply)
    common = set.intersection(*(_words(name) for name in names))
    distinct = [name for name in names if (_words(name) - common) & reply_words]
    return distinct[0] if len(distinct) == 1 else None


def resolve_reply(query: str, history: Sequence[tuple[str, str]]) -> str:
    """If the last assistant message asked which place was meant and the
    user's reply picks one of the offered places, returns the user's
    original question with the place replaced (or, if the ambiguous term
    can't be found in it, the question plus the place). Otherwise returns
    ``query`` unchanged.

    :param history: prior (role, text) turns, oldest first, without the
        current query
    """

    if not history or history[-1][0] != "assistant":
        return query
    names = _offered_names(history[-1][1])
    original = next((text for role, text in reversed(history[:-1]) if role == "user"), None)
    if len(names) < 2 or original is None:
        return query

    chosen = _chosen(query, names)
    if chosen is None:
        return query

    # The term the user originally wrote: the offered names' common start
    # ("Hoheluft-Ost", "Hoheluft-West" -> "Hoheluft").
    term = os.path.commonprefix([name.lower() for name in names]).rstrip(" -")
    if term:
        pattern = re.compile(rf"\b{re.escape(term)}\b(?![-\w])", re.IGNORECASE)
        if pattern.search(original):
            return pattern.sub(chosen, original, count=1)
    return f"{original} ({chosen})"
