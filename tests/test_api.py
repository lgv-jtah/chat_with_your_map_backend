"""End-to-end test for the /api/query AG-UI SSE endpoint."""

import json

from fastapi.testclient import TestClient

from cwym_agent.api.main import app

client = TestClient(app)


def _parse_sse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_endpoint_streams_expected_agui_event_sequence():
    with client.stream(
        "POST",
        "/api/query",
        json={
            "query": "Zeige mir Fahrradstationen in Wandsbek",
            "include_reasoning": True,
            "language": "German",
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    event_types = [event["type"] for event in events]

    # Reasoning, tool calls, state snapshot and the text message must all
    # appear, in this relative order, between RUN_STARTED and RUN_FINISHED.
    assert event_types[0] == "RUN_STARTED"
    assert event_types[-1] == "RUN_FINISHED"
    assert "RUN_ERROR" not in event_types

    def index_of(event_type: str) -> int:
        return event_types.index(event_type)

    assert index_of("REASONING_START") < index_of("REASONING_END")
    assert index_of("REASONING_END") < index_of("TOOL_CALL_START")
    assert event_types.count("TOOL_CALL_START") == 3
    assert index_of("TOOL_CALL_END") < index_of("STATE_SNAPSHOT")
    assert index_of("STATE_SNAPSHOT") < index_of("TEXT_MESSAGE_START")
    assert index_of("TEXT_MESSAGE_START") < index_of("TEXT_MESSAGE_END")
    assert index_of("TEXT_MESSAGE_END") < index_of("RUN_FINISHED")

    # The resolved location and layer must be reflected in the map state
    # snapshot, so the frontend can patch its Vuex store directly.
    snapshot_event = events[index_of("STATE_SNAPSHOT")]
    assert snapshot_event["snapshot"]["center"]["latitude"] == 53.5760029
    assert snapshot_event["snapshot"]["layers"][0]["name"] == "StadtRAD-Stationen Hamburg"

    # The streamed text message content, once joined, must be non-empty.
    text_deltas = [
        event["delta"]
        for event in events
        if event["type"] == "TEXT_MESSAGE_CONTENT"
    ]
    assert "".join(text_deltas).strip() != ""


def test_query_endpoint_default_query_still_resolves_hamburg():
    with client.stream(
        "POST",
        "/api/query",
        json={"query": "Sag mir etwas über die Stadt"},
    ) as response:
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    snapshot_event = next(event for event in events if event["type"] == "STATE_SNAPSHOT")

    assert snapshot_event["snapshot"]["center"]["latitude"] == 53.5511
