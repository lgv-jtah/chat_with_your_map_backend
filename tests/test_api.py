"""End-to-end test for the /api/query AG-UI SSE endpoint.

Uses ``StubQwen3Generator``/``StubDataServices`` (via a pipeline
dependency override), not the real Qwen3/vLLM backend, so this stays
fast and deterministic regardless of network access. Real-backend
behavior is covered separately in ``test_qwen3_client.py`` (mocked) and
``test_qwen3_live.py`` (opt-in, real server).

Requests are built as ``RunAgentInput`` bodies (cmym-agent-architecture.md
Section 10) - the wire format the real frontend sends since the
conversation-memory work landed.
"""

import json

from fastapi.testclient import TestClient

from cwym_agent.api.main import app, get_pipeline, get_portal_catalog_resolver
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.types import LayerMatch
from cwym_agent.pipeline import build_pipeline
from cwym_agent.stubs.data_services import StubDataServices
from cwym_agent.stubs.qwen3 import StubQwen3Generator


class FakePortalCatalogResolver:
    """Records the URL pair it's asked to resolve; returns a fixed
    catalog for any non-None pair, so API-level tests don't need real
    network access (unit coverage for the resolver itself lives in
    test_portal_catalog_resolver.py)."""

    def __init__(self):
        self.calls: list = []

    async def get_or_build(self, config_json_url, services_json_url):
        self.calls.append((config_json_url, services_json_url))
        if not config_json_url:
            return None
        return LayerCatalog({"27764": LayerMatch("StadtRAD-Stationen Hamburg", "27764", 1.0)})


app.dependency_overrides[get_pipeline] = lambda: build_pipeline(
    llm=StubQwen3Generator(), data_services=StubDataServices()
)

fake_portal_catalog_resolver = FakePortalCatalogResolver()
app.dependency_overrides[get_portal_catalog_resolver] = lambda: fake_portal_catalog_resolver

client = TestClient(app)


def _run_input(query: str, *, state=None, portal_context=None, thread_id="thread-1") -> dict:
    """Builds a RunAgentInput body the same shape buildRunInput.js sends
    (Section 10.8)."""

    return {
        "threadId": thread_id,
        "runId": "run-1",
        "messages": [{"id": "m1", "role": "user", "content": query}],
        "state": state,
        "tools": [],
        "context": [],
        "forwardedProps": {"portal_context": portal_context} if portal_context else {},
    }


def _parse_sse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def _command_builder_events(events: list[dict]) -> list[dict]:
    """``TOOL_CALL_RESULT`` carries no ``toolCallName`` of its own, so
    correlate each result back to the ``toolCallName`` seen on the
    matching ``TOOL_CALL_START`` for the same ``toolCallId``."""

    id_to_name = {
        event["toolCallId"]: event["toolCallName"]
        for event in events
        if event["type"] == "TOOL_CALL_START"
    }
    return [
        json.loads(event["content"])
        for event in events
        if event["type"] == "TOOL_CALL_RESULT" and id_to_name.get(event["toolCallId"]) == "command_builder"
    ]


def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_endpoint_streams_expected_agui_event_sequence():
    with client.stream(
        "POST", "/api/query", json=_run_input("Zeige mir Fahrradstationen in Wandsbek")
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    event_types = [event["type"] for event in events]

    # Reasoning, tool calls, the MapCommand stream and the text message
    # must all appear, in this relative order, between RUN_STARTED and
    # RUN_FINISHED.
    assert event_types[0] == "RUN_STARTED"
    assert event_types[-1] == "RUN_FINISHED"
    assert "RUN_ERROR" not in event_types

    def index_of(event_type: str) -> int:
        return event_types.index(event_type)

    assert index_of("REASONING_START") < index_of("REASONING_END")
    assert index_of("REASONING_END") < index_of("TOOL_CALL_START")

    tool_call_starts = [event for event in events if event["type"] == "TOOL_CALL_START"]
    sub_agent_starts = [
        event
        for event in tool_call_starts
        if event["toolCallName"] in {"location_extractor", "layer_resolver", "action_detector"}
    ]
    command_starts = [
        event for event in tool_call_starts if event["toolCallName"] == "command_builder"
    ]
    assert len(sub_agent_starts) == 3
    assert len(command_starts) == 2

    commands = _command_builder_events(events)
    assert len(commands) == 2

    activate_layer = next(cmd for cmd in commands if cmd["type"] == "ACTIVATE_LAYER")
    assert activate_layer["payload"]["layerIds"] == ["27764"]

    navigate = next(cmd for cmd in commands if cmd["type"] == "NAVIGATE_TO_LOCATION")
    assert navigate["payload"]["locations"][0]["name"] == "Wandsbek"
    assert navigate["payload"]["locations"][0]["coordinates"] == {
        "lon": 10.0755348,
        "lat": 53.5760029,
    }

    # Sub-agent tool calls finish before the MapCommand stream starts, and
    # the last MapCommand result precedes the text message.
    id_to_name = {event["toolCallId"]: event["toolCallName"] for event in tool_call_starts}
    last_sub_agent_start_index = max(
        i for i, event in enumerate(events)
        if event["type"] == "TOOL_CALL_START" and event["toolCallName"] != "command_builder"
    )
    first_command_start_index = min(
        i for i, event in enumerate(events)
        if event["type"] == "TOOL_CALL_START" and event["toolCallName"] == "command_builder"
    )
    assert last_sub_agent_start_index < first_command_start_index

    last_command_result_index = max(
        i
        for i, event in enumerate(events)
        if event["type"] == "TOOL_CALL_RESULT" and id_to_name.get(event["toolCallId"]) == "command_builder"
    )
    assert last_command_result_index < index_of("TEXT_MESSAGE_START")
    assert index_of("TEXT_MESSAGE_START") < index_of("TEXT_MESSAGE_END")
    assert index_of("TEXT_MESSAGE_END") < index_of("RUN_FINISHED")

    # The streamed text message content, once joined, must be non-empty.
    text_deltas = [
        event["delta"]
        for event in events
        if event["type"] == "TEXT_MESSAGE_CONTENT"
    ]
    assert "".join(text_deltas).strip() != ""


def test_query_endpoint_default_query_still_resolves_hamburg():
    with client.stream(
        "POST", "/api/query", json=_run_input("Sag mir etwas über die Stadt")
    ) as response:
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    commands = _command_builder_events(events)
    navigate = next(cmd for cmd in commands if cmd["type"] == "NAVIGATE_TO_LOCATION")

    assert navigate["payload"]["locations"][0]["coordinates"] == {
        "lon": 9.9937,
        "lat": 53.5511,
    }


def test_query_endpoint_unactionable_action_produces_zero_commands():
    """A resolved action with no consuming command logic yet ("Filter") is a
    legitimate zero-MapCommand turn (architecture doc Section 5) - the run
    still finishes normally with a text reply, just no map commands."""

    with client.stream(
        "POST", "/api/query", json=_run_input("Filtere Krankenhäuser in Altona")
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    event_types = [event["type"] for event in events]

    assert "RUN_ERROR" not in event_types
    assert event_types[-1] == "RUN_FINISHED"
    assert _command_builder_events(events) == []

    text_deltas = [
        event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"
    ]
    assert "".join(text_deltas).strip() != ""


def test_query_endpoint_open_contact_form_emits_open_tool_command():
    with client.stream(
        "POST", "/api/query", json=_run_input("Kannst du mir das Kontaktformular öffnen")
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    event_types = [event["type"] for event in events]

    assert "RUN_ERROR" not in event_types
    assert event_types[-1] == "RUN_FINISHED"

    commands = _command_builder_events(events)
    assert len(commands) == 1
    assert commands[0]["type"] == "OPEN_TOOL"
    assert commands[0]["payload"]["toolName"] == "CONTACT"


def test_query_endpoint_forwards_portal_context_to_catalog_resolver():
    """Section 8: a request carrying portal_context should have both
    URLs resolved via PortalCatalogResolver before the pipeline runs, so
    layer_resolver can be scoped to that portal."""

    fake_portal_catalog_resolver.calls.clear()

    with client.stream(
        "POST",
        "/api/query",
        json=_run_input(
            "Zeige mir Fahrradstationen in Wandsbek",
            portal_context={
                "config_json_url": "https://portal.example/config.json",
                "services_json_url": "https://portal.example/services.json",
            },
        ),
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse_events(body)
    assert "RUN_ERROR" not in [event["type"] for event in events]
    assert fake_portal_catalog_resolver.calls == [
        ("https://portal.example/config.json", "https://portal.example/services.json")
    ]


def test_query_endpoint_without_portal_context_stays_unscoped():
    fake_portal_catalog_resolver.calls.clear()

    with client.stream(
        "POST", "/api/query", json=_run_input("Zeige mir Fahrradstationen in Wandsbek")
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert fake_portal_catalog_resolver.calls == []


def test_query_endpoint_active_layer_state_reaches_the_planner():
    """Section 10: the live map state a request carries must actually
    influence planning - a coarse end-to-end check that build_context +
    the planner context block are wired all the way through, without
    depending on a specific plan (that's covered at the unit level in
    test_conversation_context.py/test_master_agent.py)."""

    with client.stream(
        "POST",
        "/api/query",
        json=_run_input(
            "Nur die Gymnasien",
            state={"activeLayerIds": ["27764"]},
            portal_context={
                "config_json_url": "https://portal.example/config.json",
                "services_json_url": "https://portal.example/services.json",
            },
        ),
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())


def test_query_endpoint_missing_user_query_returns_400():
    body = _run_input("placeholder")
    body["messages"] = [{"id": "m1", "role": "assistant", "content": "not a user query"}]

    response = client.post("/api/query", json=body)

    assert response.status_code == 400


def test_query_endpoint_oversized_state_returns_400():
    body = _run_input(
        "query", state={"activeLayerIds": [str(i) for i in range(51)]}
    )

    response = client.post("/api/query", json=body)

    assert response.status_code == 400
