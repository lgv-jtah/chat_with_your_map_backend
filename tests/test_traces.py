"""Tests for the trace reader CLI (``python -m cwym_agent.traces``)."""

import csv
import io
import json

from cwym_agent.observability import RunTrace
from cwym_agent.traces import main


def _record(thread_id: str, run_id: str, query: str, started: str, content: str = "full") -> dict:
    trace = RunTrace(thread_id, run_id, content=content)
    trace.set_request(query=query, history=[], mapState={"activeLayerIds": [], "filters": []})
    trace.record_llm_call("plan", 2.0, max_tokens=6144, finish_reason="length", tool_calls=[])
    record = trace.build_record("ok")
    record["startedAt"] = started
    return record


def _write(path, records, prefix=""):
    path.write_text("".join(prefix + json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def test_show_prints_the_whole_conversation_of_a_run(tmp_path):
    current = tmp_path / "traces.jsonl"
    rotated = tmp_path / "traces.jsonl.1"
    _write(rotated, [_record("thread-abc", "run-1", "Grundschulen in Hoheluft", "2026-10-23T10:00:00")])
    _write(current, [_record("thread-abc", "run-2", "auch Hoheluft-West?", "2026-10-23T10:05:00"),
                     _record("thread-other", "run-3", "Hallo", "2026-10-23T10:06:00")])
    out = io.StringIO()

    code = main(["--file", str(rotated), "--file", str(current), "show", "run-2"], out=out)

    text = out.getvalue()
    assert code == 0
    assert "Unterhaltung thread-abc" in text
    assert text.index("Grundschulen in Hoheluft") < text.index("auch Hoheluft-West?")
    assert "Hallo" not in text
    assert "finish=length" in text


def test_show_reads_docker_compose_log_output(tmp_path):
    log = tmp_path / "compose.log"
    record = _record("thread-abc", "run-1", "Schulen", "2026-10-23T10:00:00")
    log.write_text(
        "cwym-agent  | 2026-10-23 INFO cwym_agent.api.main: Run started\n"
        f"cwym-agent  | {json.dumps(record)}\n",
        encoding="utf-8",
    )
    out = io.StringIO()

    assert main(["--file", str(log), "show", "thread-a", "--json"], out=out) == 0
    assert json.loads(out.getvalue())["runId"] == "run-1"


def test_show_finds_the_ids_the_addon_copies(tmp_path):
    """CHAT-23: "Kopieren" copied threadId/runId, which matched neither id.
    The runId (what the addon copies now) and that older form both work."""

    current = tmp_path / "traces.jsonl"
    thread_id, run_id = "864d388b-4b83-4941-9ea3-d584e4d20e0c", "ce09fd26-c69e-4614-9763-5b88b1e3dacb"
    _write(current, [_record(thread_id, run_id, "Schulen", "2026-11-11T10:00:00"),
                     _record(thread_id, "aa11bb22-0000-4000-8000-000000000000", "Kitas", "2026-11-11T10:01:00")])

    for copied in (run_id, f"{thread_id}/{run_id}", f" {thread_id}/{run_id}\n", run_id[:8]):
        out = io.StringIO()
        assert main(["--file", str(current), "show", copied, "--json"], out=out) == 0, copied
        assert [json.loads(line)["runId"] for line in out.getvalue().splitlines()] == [run_id]

    assert main(["--file", str(current), "show", f"other-thread/{run_id}"], out=io.StringIO()) == 1


def test_show_unknown_id_fails(tmp_path):
    current = tmp_path / "traces.jsonl"
    _write(current, [_record("thread-abc", "run-1", "Schulen", "2026-10-23T10:00:00")])

    assert main(["--file", str(current), "show", "nope"], out=io.StringIO()) == 1


def test_queries_exports_one_row_per_turn(tmp_path, monkeypatch):
    current = tmp_path / "traces.jsonl"
    _write(current, [
        _record("thread-abc", "run-2", "auch Hoheluft-West?", "2026-10-23T10:05:00"),
        _record("thread-abc", "run-1", "Grundschulen in Hoheluft", "2026-10-23T10:00:00"),
        _record("thread-xyz", "run-3", "Geheim", "2026-10-23T10:06:00", content="metadata"),
    ])
    monkeypatch.setenv("CWYM_TRACE_FILE", str(current))
    out = io.StringIO()

    assert main(["queries"], out=out) == 0

    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert [(r["query"], r["turn"]) for r in rows] == [
        ("Grundschulen in Hoheluft", "1"),
        ("auch Hoheluft-West?", "2"),
        ("<6 chars, not logged>", "1"),
    ]
    assert rows[0]["planFinishReason"] == "length"
