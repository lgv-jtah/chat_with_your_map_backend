"""Unit tests for ``cwym_agent.observability``: log correlation ids,
the per-turn ``RunTrace`` record (built from the AG-UI events the
bridge emits) and the JSONL trace file."""

import contextvars
import json
import logging
import threading

import pytest
from ag_ui.core import RunErrorEvent

from cwym_agent import observability
from cwym_agent.bridge import AGUIEventBridge
from cwym_agent.observability import LoggingSettings, RunTrace, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging_config():
    """Tests reconfigure logging (file sink, content mode) - put the
    default back afterwards so other test modules aren't affected."""

    yield
    configure_logging(LoggingSettings(trace_file=None, trace_content="full", format="text", level="INFO"))


def _traced_bridge(content: str = "full") -> tuple[AGUIEventBridge, RunTrace]:
    trace = RunTrace("thread-1", "run-1", content=content)
    return AGUIEventBridge(thread_id="thread-1", run_id="run-1", trace=trace), trace


def _emit_a_turn(bridge: AGUIEventBridge) -> None:
    """One filter turn, as the pipeline emits it."""

    bridge.run_started()
    bridge.reasoning("Der Nutzer will Schulen in Hoheluft-West.")
    call_id = bridge.tool_call_start("location_extractor", {"query": "Hoheluft-West"})
    bridge.tool_call_result(
        call_id,
        [{"name": "Hoheluft-West", "kind": "stadtteil", "boundary": {"type": "Polygon", "coordinates": [[[0, 0]] * 500]}}],
    )
    call_id = bridge.tool_call_start("filter_agent", {"query": "Stadtteilschulen in Hoheluft-West", "layerId": "8712"})
    bridge.tool_call_result(call_id, {"layer_id": "8712", "number_matched": 1, "feature_count": 1})
    bridge.command(
        {
            "commandId": "cmd-1",
            "type": "APPLY_FILTER",
            "payload": {
                "layerId": "8712",
                "features": {"type": "FeatureCollection", "features": [{"type": "Feature"}] * 3},
                "filterSpec": {"conditions": [], "area": {"name": "Hoheluft-West", "kind": "stadtteil"}},
            },
        }
    )
    bridge.text_message(["Ich habe ", "gefiltert."])
    bridge.run_finished()


class TestRunTraceFromEvents:
    def test_record_contains_the_whole_turn(self):
        bridge, trace = _traced_bridge()
        trace.set_request(query="und Hoheluft-West?", history=[{"role": "user", "text": "Grundschulen"}])
        trace.set_planning_prompt("## Aktueller Kartenzustand ...")
        trace.record_llm_call("plan", 1.5, max_tokens=6144, finish_reason="tool_calls")

        _emit_a_turn(bridge)
        record = trace.build_record("ok")

        assert record["type"] == "run_trace"
        assert record["threadId"] == "thread-1"
        assert record["runId"] == "run-1"
        assert record["status"] == "ok"
        assert record["request"]["query"] == "und Hoheluft-West?"
        assert record["planning"]["prompt"] == "## Aktueller Kartenzustand ..."
        assert record["planning"]["reasoning"] == "Der Nutzer will Schulen in Hoheluft-West."
        assert record["summary"]["steps"] == ["location_extractor", "filter_agent"]
        assert record["summary"]["commands"] == ["APPLY_FILTER"]
        assert record["summary"]["planFinishReason"] == "tool_calls"
        assert record["steps"][1]["args"] == {"query": "Stadtteilschulen in Hoheluft-West", "layerId": "8712"}
        assert record["steps"][1]["result"]["number_matched"] == 1
        assert all(isinstance(step["durationMs"], int) for step in record["steps"])
        assert record["answer"] == "Ich habe gefiltert."
        assert "error" not in record

    def test_geometry_and_features_are_summarized_not_copied(self):
        bridge, trace = _traced_bridge()
        _emit_a_turn(bridge)

        record = trace.build_record("ok")

        assert record["steps"][0]["result"][0]["boundary"] == {"type": "Polygon", "omitted": True}
        assert record["commands"][0]["payload"]["features"] == {"type": "FeatureCollection", "featureCount": 3}
        assert record["commands"][0]["payload"]["filterSpec"]["area"]["name"] == "Hoheluft-West"

    def test_empty_plan_turn_is_still_a_complete_record(self):
        """The Hoheluft-West case: planning only, no steps, no commands."""

        bridge, trace = _traced_bridge()
        bridge.run_started()
        bridge.reasoning("...")
        trace.record_llm_call("plan", 240.0, max_tokens=6144, finish_reason="length")
        bridge.text_message(["Leider konnte ich das nicht umsetzen."])
        bridge.run_finished()

        record = trace.build_record("ok")

        assert record["summary"]["steps"] == []
        assert record["summary"]["commands"] == []
        assert record["summary"]["planFinishReason"] == "length"
        assert record["summary"]["llmMs"] == 240_000

    def test_error_carries_the_traceback(self):
        _, trace = _traced_bridge()
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            error = exc

        record = trace.build_record("error", error)

        assert record["status"] == "error"
        assert record["error"]["type"] == "RuntimeError"
        assert record["error"]["message"] == "boom"
        assert "Traceback" in record["error"]["traceback"]

    def test_run_error_event_without_exception_is_kept(self):
        bridge, trace = _traced_bridge()
        bridge.emit(RunErrorEvent(message="upstream failed"))

        assert trace.build_record("error")["error"]["message"] == "upstream failed"

    def test_long_strings_are_capped(self):
        _, trace = _traced_bridge()
        trace.set_planning_prompt("x" * 50_000)

        prompt = trace.build_record("ok")["planning"]["prompt"]

        assert len(prompt) < 21_000
        assert "chars truncated" in prompt

    def test_a_broken_event_does_not_raise(self):
        _, trace = _traced_bridge()

        trace.on_event(object())  # no .type - must be ignored, not raise


class TestMetadataMode:
    def test_no_user_text_ends_up_in_the_record(self):
        bridge, trace = _traced_bridge(content="metadata")
        trace.set_request(
            query="Wo wohnt Max Mustermann, Musterstraße 1?",
            history=[{"role": "user", "text": "Musterstraße 1"}],
            mapState={"activeLayerIds": ["8712"], "filters": [], "lastLocation": {"name": "Musterstraße 1", "kind": "address"}},
            stateIn={"lastLocation": {"name": "Musterstraße 1", "kind": "address"}},
            catalogLayers=12,
        )
        trace.set_planning_prompt("Nutzeranfrage: Musterstraße 1")
        trace.record_llm_call("text_response", 1.0, prompt="Musterstraße 1", output="Musterstraße 1")
        _emit_a_turn(bridge)
        bridge.command(
            {
                "commandId": "cmd-2",
                "type": "NAVIGATE_TO_LOCATION",
                "payload": {"locations": [{"name": "Musterstraße 1", "coordinates": {"lon": 9.987654, "lat": 53.581234}}]},
            }
        )

        record = trace.build_record("ok")
        serialized = json.dumps(record, ensure_ascii=False)

        assert "Musterstraße" not in serialized
        assert "Mustermann" not in serialized
        assert "Hoheluft-West" not in serialized
        assert "53.581234" not in serialized
        assert record["contentMode"] == "metadata"
        # Structure, ids, counts and timings stay.
        assert record["request"]["query"] == {"chars": len("Wo wohnt Max Mustermann, Musterstraße 1?")}
        assert record["request"]["history"] == [{"role": "user", "text": {"chars": 14}}]
        assert record["request"]["catalogLayers"] == 12
        assert record["request"]["mapState"] == {
            "activeLayerIds": ["8712"],
            "filterLayerIds": [],
            "hasLastLocation": True,
        }
        assert record["summary"]["steps"] == ["location_extractor", "filter_agent"]
        assert record["commands"][0]["type"] == "APPLY_FILTER"
        assert record["answer"] == {"chars": len("Ich habe gefiltert.")}


class TestFinish:
    def test_writes_exactly_one_json_line(self):
        records: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Collect()
        trace_logger = logging.getLogger(observability.TRACE_LOGGER_NAME)
        trace_logger.addHandler(handler)
        try:
            bridge, trace = _traced_bridge()
            _emit_a_turn(bridge)
            first = trace.finish("ok")
            second = trace.finish("ok")
        finally:
            trace_logger.removeHandler(handler)

        assert first is not None
        assert second is None
        assert len(records) == 1
        assert json.loads(records[0])["runId"] == "run-1"


class TestConfigureLogging:
    def test_trace_file_gets_valid_jsonl(self, tmp_path):
        path = tmp_path / "traces" / "traces.jsonl"
        configure_logging(LoggingSettings(trace_file=str(path), trace_content="full", format="text", level="INFO"))

        for run_id in ("run-a", "run-b"):
            trace = RunTrace("thread-1", run_id)
            trace.set_request(query=f"Frage {run_id}")
            trace.finish("ok")

        lines = path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["runId"] for line in lines] == ["run-a", "run-b"]
        assert json.loads(lines[0])["request"]["query"] == "Frage run-a"

    def test_reconfiguring_does_not_duplicate_handlers(self):
        settings = LoggingSettings(trace_file=None, trace_content="full", format="text", level="INFO")
        configure_logging(settings)
        configure_logging(settings)

        own = [h for h in logging.getLogger().handlers if getattr(h, "_cwym", False)]
        own_trace = [h for h in logging.getLogger(observability.TRACE_LOGGER_NAME).handlers if getattr(h, "_cwym", False)]
        assert len(own) == 1
        assert len(own_trace) == 1

    def test_invalid_env_value_fails_fast(self, monkeypatch):
        monkeypatch.setenv("CWYM_TRACE_CONTENT", "everything")

        with pytest.raises(ValueError, match="CWYM_TRACE_CONTENT"):
            LoggingSettings()

    def test_json_format_carries_ids(self):
        formatter = observability.JsonFormatter()
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        context = contextvars.copy_context()
        context.run(observability.bind_ids, "thread-1", "run-1")
        context.run(observability.ContextFilter().filter, record)

        entry = json.loads(formatter.format(record))

        assert entry["message"] == "hello world"
        assert entry["threadId"] == "thread-1"
        assert entry["runId"] == "run-1"


class TestCorrelation:
    def test_ids_reach_records_from_a_worker_thread_and_stay_per_context(self):
        """The API runs the pipeline in a thread inside a copied context -
        the ids must arrive there, and must not leak into another run."""

        seen: dict[str, tuple] = {}
        context_filter = observability.ContextFilter()

        def work(name):
            record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
            context_filter.filter(record)
            seen[name] = (record.thread_id, record.run_id, record.ids)

        def run(name, thread_id, run_id):
            context = contextvars.copy_context()
            context.run(observability.start_run, thread_id, run_id)
            worker = threading.Thread(target=context.run, args=(work, name))
            worker.start()
            worker.join()

        run("a", "thread-aaaaaaaaaa", "run-aaaaaaaaaa")
        run("b", "thread-bbbbbbbbbb", "run-bbbbbbbbbb")
        work("outside")

        assert seen["a"][:2] == ("thread-aaaaaaaaaa", "run-aaaaaaaaaa")
        assert seen["a"][2] == "[thread-a/run-aaaa] "
        assert seen["b"][:2] == ("thread-bbbbbbbbbb", "run-bbbbbbbbbb")
        assert seen["outside"] == (None, None, "")

    def test_helpers_are_noops_outside_a_run(self):
        observability.record_llm_call("plan", 1.0)
        observability.set_planning_prompt("x")
        observability.note("x")

        assert observability.current_trace() is None
