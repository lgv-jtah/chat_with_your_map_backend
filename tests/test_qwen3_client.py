"""Unit tests for ``Qwen3Client`` against a mocked HTTP transport,
covering the streaming reasoning/tool-call parsing logic deterministically
and without any real network access.

Uses ``httpx2.MockTransport`` (the vendored HTTP client the ``openai``
SDK is built on in this environment) injected via ``Qwen3Client``'s
``http_client`` constructor parameter, rather than a request-mocking
library such as ``respx`` - which targets the standalone ``httpx``
package and does not intercept ``httpx2`` traffic.

Real end-to-end behavior against the live vLLM server is covered
separately (and only opt-in) in ``test_qwen3_live.py``.
"""

import json

import httpx2

from cwym_agent.llm.qwen3_client import Qwen3Client
from cwym_agent.settings import Qwen3Settings

_BASE_URL = "http://test-vllm.invalid/v1"
_MODEL = "Qwen/Qwen3.8-27B"


def _settings() -> Qwen3Settings:
    return Qwen3Settings(base_url=_BASE_URL, model=_MODEL, api_key="EMPTY")


def _sse(*deltas: dict) -> str:
    """Build an SSE body from a sequence of ``choices[0].delta`` dicts."""

    lines = []
    for delta in deltas:
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": _MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def _client_for(body: str) -> Qwen3Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return Qwen3Client(_settings(), http_client=http_client)


def _tool_call_delta(index: int, call_id: str | None, name: str | None, arguments: str | None) -> dict:
    function: dict = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments

    tool_call: dict = {"index": index, "function": function}
    if call_id is not None:
        tool_call["id"] = call_id

    return {"tool_calls": [tool_call]}


class TestPlan:
    def test_streams_reasoning_and_builds_steps_from_tool_calls(self):
        body = _sse(
            {"role": "assistant", "content": ""},
            {"reasoning": "Ich muss "},
            {"reasoning": "den Ort finden."},
            _tool_call_delta(0, "call_1", "location_extractor", ""),
            _tool_call_delta(0, None, None, '{"query": "Altona"}'),
            _tool_call_delta(1, "call_2", "layer_resolver", ""),
            _tool_call_delta(1, None, None, '{"query": "Fahrradstationen"}'),
        )

        client = _client_for(body)
        reasoning_chunks: list[str] = []
        result = client.plan(
            "Zeige Fahrradstationen in Altona",
            streaming_callback=lambda chunk: reasoning_chunks.append(chunk.content),
        )

        assert "".join(reasoning_chunks) == "Ich muss den Ort finden."
        assert result == {
            "steps": [
                {"agent_name": "location_extractor", "args": {"query": "Altona"}},
                {"agent_name": "layer_resolver", "args": {"query": "Fahrradstationen"}},
            ]
        }

    def test_arguments_split_across_many_chunks_are_reassembled(self):
        body = _sse(
            _tool_call_delta(0, "call_1", "action_detector", ""),
            _tool_call_delta(0, None, None, '{"que'),
            _tool_call_delta(0, None, None, 'ry": "navi'),
            _tool_call_delta(0, None, None, 'giere"}'),
        )

        client = _client_for(body)
        result = client.plan("navigiere nach Altona")

        assert result == {
            "steps": [{"agent_name": "action_detector", "args": {"query": "navigiere"}}]
        }

    def test_unknown_tool_name_is_skipped(self):
        body = _sse(
            _tool_call_delta(0, "call_1", "location_extractor", '{"query": "Altona"}'),
            _tool_call_delta(1, "call_2", "hallucinated_tool", '{"query": "x"}'),
        )

        client = _client_for(body)
        result = client.plan("irgendeine Anfrage")

        assert result == {
            "steps": [{"agent_name": "location_extractor", "args": {"query": "Altona"}}]
        }

    def test_malformed_json_arguments_are_skipped(self):
        body = _sse(
            _tool_call_delta(0, "call_1", "location_extractor", "{not valid json"),
        )

        client = _client_for(body)
        result = client.plan("irgendeine Anfrage")

        assert result == {"steps": []}

    def test_no_tool_calls_yields_empty_steps(self):
        body = _sse(
            {"role": "assistant", "content": ""},
            {"reasoning": "Das ist Small Talk, kein Kartenbezug."},
            {"content": "Hallo!"},
        )

        client = _client_for(body)
        result = client.plan("Hallo, wie geht's?")

        assert result == {"steps": []}


class TestGenerate:
    def test_streams_content_only_and_discards_reasoning(self):
        body = _sse(
            {"role": "assistant", "content": ""},
            {"reasoning": "Der Nutzer fragt nach Fahrradstationen."},
            {"content": "Hier "},
            {"content": "sind "},
            {"content": "die Fahrradstationen."},
        )

        client = _client_for(body)
        content_chunks: list[str] = []
        result = client.generate(
            "Formuliere eine Antwort.",
            streaming_callback=lambda chunk: content_chunks.append(chunk.content),
        )

        assert result == "Hier sind die Fahrradstationen."
        assert content_chunks == ["Hier ", "sind ", "die Fahrradstationen."]
