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
_PLAN = {"system_prompt": "Plane.", "tools": []}
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
            **_PLAN,
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
        result = client.plan("navigiere nach Altona", **_PLAN)

        assert result == {
            "steps": [{"agent_name": "action_detector", "args": {"query": "navigiere"}}]
        }

    def test_unknown_tool_name_is_skipped(self):
        body = _sse(
            _tool_call_delta(0, "call_1", "location_extractor", '{"query": "Altona"}'),
            _tool_call_delta(1, "call_2", "hallucinated_tool", '{"query": "x"}'),
        )

        client = _client_for(body)
        result = client.plan("irgendeine Anfrage", **_PLAN)

        assert result == {
            "steps": [{"agent_name": "location_extractor", "args": {"query": "Altona"}}]
        }

    def test_malformed_json_arguments_are_skipped(self):
        body = _sse(
            _tool_call_delta(0, "call_1", "location_extractor", "{not valid json"),
        )

        client = _client_for(body)
        result = client.plan("irgendeine Anfrage", **_PLAN)

        assert result == {"steps": []}

    def test_no_tool_calls_yields_empty_steps(self):
        body = _sse(
            {"role": "assistant", "content": ""},
            {"reasoning": "Das ist Small Talk, kein Kartenbezug."},
            {"content": "Hallo!"},
        )

        client = _client_for(body)
        result = client.plan("Hallo, wie geht's?", **_PLAN)

        assert result == {"steps": []}


class TestGenerate:
    def test_reasoning_can_be_switched_off_per_call(self):
        bodies = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            bodies.append(json.loads(request.content))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse({"content": "Ok."}))

        client = Qwen3Client(_settings(), http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        client.generate("PROMPT")
        client.generate("PROMPT", reasoning=False)

        assert "chat_template_kwargs" not in bodies[0]
        assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}

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
            client.plan("Zeige Fahrradstationen in Altona", **_PLAN)

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


class TestRunTraceRecording:
    """Inside a traced run (cwym_agent.observability), every LLM call
    lands in the run's trace - with duration, tokens and finish_reason,
    and for the planner the raw tool calls before any are dropped."""

    def _traced(self):
        import contextvars

        from cwym_agent import observability

        context = contextvars.copy_context()
        trace = context.run(observability.start_run, "thread-x", "run-x")
        return context, trace

    def test_plan_records_raw_tool_calls_including_malformed_ones(self):
        body = _sse(
            _tool_call_delta(0, "call_1", "location_extractor", "{not valid json"),
            finish_reason="length",
            usage={"prompt_tokens": 100, "completion_tokens": 6144, "total_tokens": 6244},
        )
        client = _client_for(body)
        context, trace = self._traced()

        result = context.run(lambda: client.plan("irgendeine Anfrage", **_PLAN))

        assert result == {"steps": []}
        [call] = trace.llm_calls
        assert call["label"] == "plan"
        assert call["finishReason"] == "length"
        assert call["usage"] == {"promptTokens": 100, "completionTokens": 6144}
        assert call["toolCalls"] == [{"name": "location_extractor", "arguments": "{not valid json"}]
        assert call["durationMs"] >= 0

    def test_generate_records_prompt_and_output(self):
        body = _sse(
            {"content": "Hallo"},
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )
        client = _client_for(body)
        context, trace = self._traced()

        context.run(client.generate, "Formuliere eine Antwort.", label="text_response")

        [call] = trace.llm_calls
        assert call["label"] == "text_response"
        assert call["prompt"] == "Formuliere eine Antwort."
        assert call["output"] == "Hallo"

    def test_failed_call_is_recorded_and_reraised(self, caplog):
        import pytest

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(400, json={"error": {"message": "bad request"}})

        client = Qwen3Client(_settings(), http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        context, trace = self._traced()

        with caplog.at_level("WARNING", logger="cwym_agent.llm.qwen3_client"):
            with pytest.raises(Exception):
                context.run(client.generate, "Formuliere eine Antwort.", label="text_response")

        [call] = trace.llm_calls
        assert call["label"] == "text_response"
        assert "error" in call
        assert any("failed after" in r.message for r in caplog.records)

    def test_outside_a_traced_run_nothing_is_recorded(self):
        body = _sse({"content": "Hallo"}, finish_reason="stop")
        client = _client_for(body)

        # Must simply not fail - there is no trace to record into.
        assert client.generate("Formuliere eine Antwort.") == "Hallo"
