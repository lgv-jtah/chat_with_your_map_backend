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


def _sse(*deltas: dict, finish_reason: str | None = None, usage: dict | None = None) -> str:
    """Build an SSE body from a sequence of ``choices[0].delta`` dicts.

    ``finish_reason`` is attached to the last delta chunk (matching how
    a real OpenAI-compatible stream ends a choice). ``usage``, if given,
    is appended as its own trailing chunk with empty ``choices`` -
    matching vLLM/OpenAI's ``stream_options={"include_usage": True}``
    behavior, which this client always requests now.
    """

    lines = []
    for index, delta in enumerate(deltas):
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": _MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason if index == len(deltas) - 1 else None,
                }
            ],
        }
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    if usage is not None:
        usage_chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": _MODEL,
            "choices": [],
            "usage": usage,
        }
        lines.append(f"data: {json.dumps(usage_chunk)}\n\n")
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


class TestUsageLogging:
    """Dev-facing token usage logging (added after three separate live
    truncation bugs this session where a call silently ran out of
    completion tokens with nothing in the logs pointing at why)."""

    def test_plan_logs_usage_at_info_on_clean_finish(self, caplog):
        body = _sse(
            _tool_call_delta(0, "call_1", "location_extractor", '{"query": "Altona"}'),
            finish_reason="tool_calls",
            usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
        )
        client = _client_for(body)

        with caplog.at_level("INFO", logger="cwym_agent.llm.qwen3_client"):
            client.plan("Zeige Fahrradstationen in Altona")

        [record] = [r for r in caplog.records if r.name == "cwym_agent.llm.qwen3_client"]
        assert record.levelname == "INFO"
        assert "plan" in record.message
        assert "prompt=100" in record.message
        assert "completion=40" in record.message
        assert "finish_reason=tool_calls" in record.message

    def test_generate_logs_warning_when_truncated(self, caplog):
        body = _sse(
            {"content": "Ich zeige dir"},
            finish_reason="length",
            usage={"prompt_tokens": 300, "completion_tokens": 512, "total_tokens": 812},
        )
        client = _client_for(body)

        with caplog.at_level("INFO", logger="cwym_agent.llm.qwen3_client"):
            client.generate("Formuliere eine Antwort.", max_tokens=512, label="text_response")

        [record] = [r for r in caplog.records if r.name == "cwym_agent.llm.qwen3_client"]
        assert record.levelname == "WARNING"
        assert "text_response" in record.message
        assert "TRUNCATED" in record.message
        assert "completion=512/512" in record.message

    def test_generate_uses_the_given_label(self, caplog):
        body = _sse(
            {"content": "Gymnasium"},
            finish_reason="stop",
            usage={"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
        )
        client = _client_for(body)

        with caplog.at_level("INFO", logger="cwym_agent.llm.qwen3_client"):
            client.generate("Extrahiere Bedingungen.", label="filter_condition_extraction")

        [record] = [r for r in caplog.records if r.name == "cwym_agent.llm.qwen3_client"]
        assert "filter_condition_extraction" in record.message

    def test_missing_usage_falls_back_gracefully(self, caplog):
        """A server without stream_options support (or an older proxy)
        simply never sends a usage chunk - must not crash."""

        body = _sse({"content": "Hallo"}, finish_reason="stop", usage=None)
        client = _client_for(body)

        with caplog.at_level("INFO", logger="cwym_agent.llm.qwen3_client"):
            result = client.generate("Formuliere eine Antwort.")

        assert result == "Hallo"
        [record] = [r for r in caplog.records if r.name == "cwym_agent.llm.qwen3_client"]
        assert "no usage reported" in record.message
