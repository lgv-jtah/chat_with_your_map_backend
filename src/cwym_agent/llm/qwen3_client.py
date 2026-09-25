"""Real Qwen3 client, talking to an OpenAI-compatible vLLM server.

Same public interface as ``cwym_agent.stubs.qwen3.StubQwen3Generator``
(``plan()`` / ``generate()``, both accepting an optional
``streaming_callback`` that receives Haystack ``StreamingChunk``
objects) so it is a drop-in replacement for the calling components.

Qwen3, served via vLLM, returns its ``<think>`` reasoning trace as a
separate ``reasoning`` field on the chat completion message/delta -
this is a vLLM/Qwen3 extension, not part of the official OpenAI schema,
which is why this is a small custom wrapper around the ``openai`` SDK
rather than Haystack's built-in ``OpenAIChatGenerator`` (which only
reads ``.content`` and ``.tool_calls``, silently dropping ``reasoning``).
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from haystack.dataclasses import StreamingChunk
from openai import OpenAI

from cwym_agent.settings import Qwen3Settings
from cwym_agent.sub_agents import SUB_AGENT_NAMES, as_openai_tools

logger = logging.getLogger(__name__)

StreamingCallback = Callable[[StreamingChunk], None]

_PLAN_SYSTEM_PROMPT = (
    "Du bist der Planungs-Agent von CWYM (Chat with your Map), einem "
    "Chatbot für Hamburgs Masterportal-Kartensystem. Analysiere die "
    "deutsche Nutzeranfrage und rufe die passenden Werkzeuge auf, um "
    "Ort, Kartenebene und/oder gewünschte Aktion aufzulösen. Rufe nur "
    "die Werkzeuge auf, die für die Anfrage tatsächlich relevant sind. "
    "Wenn die Anfrage keinen Bezug zur Karte hat (z.B. Small Talk), rufe "
    "kein Werkzeug auf."
)

_GENERATE_SYSTEM_PROMPT = (
    "Du bist der Antwort-Agent von CWYM (Chat with your Map). Formuliere "
    "eine kurze, freundliche und natürliche deutsche Antwort basierend "
    "auf den bereitgestellten Informationen. Antworte ausschließlich auf "
    "Deutsch, ohne die interne Verarbeitung zu erwähnen."
)


class _ToolCallAccumulator:
    """Assembles streamed OpenAI tool-call deltas into complete calls.

    Tool call arguments arrive as incremental string fragments across
    multiple chunks, keyed by the tool call's ``index`` within the
    response (not its final ``id``, which may only appear on the first
    fragment).
    """

    def __init__(self) -> None:
        self._by_index: dict[int, dict] = {}

    def add(self, tool_call_deltas) -> None:
        for delta in tool_call_deltas:
            entry = self._by_index.setdefault(
                delta.index, {"id": None, "name": None, "arguments": ""}
            )
            if delta.id:
                entry["id"] = delta.id
            function = delta.function
            if function is not None:
                if function.name:
                    entry["name"] = function.name
                if function.arguments:
                    entry["arguments"] += function.arguments

    def as_steps(self) -> list[dict]:
        steps = []
        for entry in self._by_index.values():
            name = entry["name"]
            if name is None:
                continue
            if name not in SUB_AGENT_NAMES:
                logger.warning("Qwen3 requested an unknown sub-agent %r, skipping", name)
                continue

            args_str = entry["arguments"] or "{}"
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                logger.warning(
                    "Qwen3 returned malformed JSON arguments for %r (%r), skipping",
                    name,
                    args_str,
                )
                continue

            steps.append({"agent_name": name, "args": args})

        return steps


def _log_completion_usage(label: str, max_tokens: int, usage, finish_reason: str | None) -> None:
    """Dev-facing visibility into what each LLM call actually burned,
    against the budget it was given - added after three separate live
    bugs this session where a call silently ran out of completion
    tokens mid-reasoning, producing an empty or truncated result with
    nothing in the logs pointing at why. ``finish_reason == "length"``
    is exactly that signal, so it's logged loudly (WARNING) rather than
    buried at the same level as a normal completion.

    ``usage`` may be ``None`` if the server didn't return a final usage
    chunk (e.g. ``stream_options`` unsupported by an older server) -
    logged as a fallback with only what's known.
    """

    if usage is None:
        logger.info("Qwen3 %s: finish_reason=%s (no usage reported)", label, finish_reason)
        return

    pct = (usage.completion_tokens / max_tokens * 100) if max_tokens else 0
    message = (
        "Qwen3 %s: prompt=%d completion=%d/%d (%.0f%%) finish_reason=%s"
    )
    args = (label, usage.prompt_tokens, usage.completion_tokens, max_tokens, pct, finish_reason)
    if finish_reason == "length":
        logger.warning(message + " - TRUNCATED, ran out of completion tokens", *args)
    else:
        logger.info(message, *args)


class Qwen3Client:
    """Real Qwen3 (reasoning/generation) backend, served via vLLM."""

    def __init__(self, settings: Qwen3Settings | None = None, http_client: Any = None):
        self.settings = settings or Qwen3Settings()
        self._client = OpenAI(
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            timeout=self.settings.timeout,
            http_client=http_client,
        )

    def plan(
        self,
        query: str,
        streaming_callback: StreamingCallback | None = None,
    ) -> dict:
        """Ask Qwen3 which sub-agents to call to resolve ``query``.

        Streams the model's reasoning trace into ``streaming_callback``
        as it arrives, and returns ``{"steps": [...]}`` built from
        whichever tools the model actually decided to call (zero, one,
        several, possibly the same tool more than once).
        """

        stream = self._client.chat.completions.create(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            tools=as_openai_tools(),
            tool_choice="auto",
            max_tokens=self.settings.max_tokens_plan,
            stream=True,
            stream_options={"include_usage": True},
        )

        accumulator = _ToolCallAccumulator()
        usage = None
        finish_reason = None

        for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta

            reasoning_delta = getattr(delta, "reasoning", None)
            if reasoning_delta and streaming_callback:
                streaming_callback(StreamingChunk(content=reasoning_delta))

            if delta.tool_calls:
                accumulator.add(delta.tool_calls)

        _log_completion_usage("plan", self.settings.max_tokens_plan, usage, finish_reason)
        return {"steps": accumulator.as_steps()}

    def generate(
        self,
        prompt: str,
        streaming_callback: StreamingCallback | None = None,
        max_tokens: int | None = None,
        label: str = "generate",
    ) -> str:
        """Generate a German reply/short answer for the built prompt.

        Only the ``content`` stream is forwarded to ``streaming_callback``
        - the reasoning trace Qwen3 also produces for this step is
        intentionally discarded, since ``REASONING_*`` AG-UI events are
        scoped to the planning stage only (see architecture doc).

        :param max_tokens: Overrides ``settings.max_tokens_generate``.
            Qwen3 produces its reasoning trace *before* the final answer
            even for short classification-style prompts (e.g. "reply
            with just a number") - callers whose prompts invite lengthy
            reasoning (as opposed to final natural-language replies,
            which tend to need less) should pass a larger budget here,
            or the reasoning can consume the whole budget and leave no
            room for the actual answer.
        :param label: identifies this call in the usage log (e.g.
            "text_response", "layer_disambiguation") - this method is
            shared by several different callers with very different
            prompts/budgets, so the generic "generate" default isn't
            enough on its own to tell them apart in the logs.
        """

        effective_max_tokens = max_tokens if max_tokens is not None else self.settings.max_tokens_generate
        stream = self._client.chat.completions.create(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=effective_max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )

        chunks: list[str] = []
        usage = None
        finish_reason = None

        for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta

            content_delta = delta.content
            if content_delta:
                chunks.append(content_delta)
                if streaming_callback:
                    streaming_callback(StreamingChunk(content=content_delta))

        _log_completion_usage(label, effective_max_tokens, usage, finish_reason)
        return "".join(chunks)
