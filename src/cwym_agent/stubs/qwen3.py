"""Stub Qwen3 (reasoning LLM) client.

Fakes both responsibilities the real Qwen3 backend is expected to provide:

- ``plan``: query planning with a visible ``<think>`` reasoning trace,
  producing an execution plan (which sub-agents to call, in which order).
- ``generate``: natural language generation of the final German reply.

Both methods accept an optional ``streaming_callback`` and emit real
Haystack ``StreamingChunk`` objects (rather than raw strings), matching
the behavior of Haystack chat generators such as
``HuggingFaceAPIChatGenerator`` or an OpenAI-compatible wrapper - this
stub can be swapped for the real generator without touching the calling
components.
"""

from collections.abc import Callable

from haystack.dataclasses import StreamingChunk


StreamingCallback = Callable[[StreamingChunk], None]


def _stream(text: str, streaming_callback: StreamingCallback | None) -> str:
    """Split text into whitespace-preserving tokens and stream them."""

    if streaming_callback is None:
        return text

    tokens = text.split(" ")
    for index, token in enumerate(tokens):
        chunk_content = token if index == len(tokens) - 1 else f"{token} "
        streaming_callback(StreamingChunk(content=chunk_content))

    return text


class StubQwen3Generator:
    """Fake stand-in for the real Qwen3 reasoning/generation backend."""

    def plan(
        self,
        query: str,
        streaming_callback: StreamingCallback | None = None,
    ) -> dict:
        """Produce a canned execution plan plus a fake reasoning trace."""

        reasoning = (
            f"Die Anfrage \"{query}\" scheint sich auf einen Ort und eine "
            "Kartenebene zu beziehen. Ich muss den Ort extrahieren, die "
            "passende Kartenebene per Vektorsuche auflösen und die "
            "gewünschte Aktion bestimmen, bevor ich antworte."
        )
        _stream(reasoning, streaming_callback)

        return {
            "steps": [
                {"agent_name": "location_extractor", "args": {"query": query}},
                {"agent_name": "layer_resolver", "args": {"query": query}},
                {"agent_name": "action_detector", "args": {"query": query}},
            ]
        }

    def generate(
        self,
        prompt: str,
        streaming_callback: StreamingCallback | None = None,
        max_tokens: int | None = None,
        label: str = "generate",
    ) -> str:
        """Produce a canned German reply based on the built prompt.

        ``max_tokens``/``label`` accepted (and ignored) only to match
        the real ``Qwen3Client.generate()`` signature - this stub never
        truncates or logs usage, so there's nothing for them to do
        here.
        """

        text = prompt
        _stream(text, streaming_callback)
        return text
