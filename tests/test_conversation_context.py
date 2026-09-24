"""Unit tests for ``build_context``/``render_context_block``
(cmym-agent-architecture.md Section 10) - the conversation-memory
validation and prompt-rendering logic, independent of the pipeline.
"""

import pytest
from ag_ui.core import AssistantMessage, RunAgentInput, SystemMessage, UserMessage
from pydantic import ValidationError

from cwym_agent.geodata.conversation_context import (
    ConversationContext,
    MapStateIn,
    build_context,
    current_query,
    render_context_block,
)
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.types import LayerMatch


def _run_input(*, state=None, messages, forwarded_props=None) -> RunAgentInput:
    return RunAgentInput(
        thread_id="t1",
        run_id="r1",
        state=state,
        messages=messages,
        tools=[],
        context=[],
        forwarded_props=forwarded_props or {},
    )


def _catalog(*ids_and_names: tuple[str, str]) -> LayerCatalog:
    return LayerCatalog({i: LayerMatch(name=n, layer_id=i, confidence=1.0) for i, n in ids_and_names})


class TestCurrentQuery:
    def test_last_user_message_is_the_query(self):
        inp = _run_input(messages=[UserMessage(id="1", content="Wo finde ich Schulen?")])

        assert current_query(inp) == "Wo finde ich Schulen?"

    def test_no_messages_raises(self):
        with pytest.raises(ValueError):
            current_query(_run_input(messages=[]))

    def test_last_message_not_from_user_raises(self):
        inp = _run_input(messages=[AssistantMessage(id="1", content="Klar.")])

        with pytest.raises(ValueError):
            current_query(inp)


class TestBuildContext:
    def test_empty_state_and_no_history_yields_empty_context(self):
        inp = _run_input(messages=[UserMessage(id="1", content="Schulen?")])

        ctx = build_context(inp, catalog=None)

        assert ctx.map_state.active_layer_ids == []
        assert ctx.map_state.filters == []
        assert ctx.history == []

    def test_unknown_layer_id_is_dropped(self):
        inp = _run_input(
            state={"activeLayerIds": ["1732", "999999"]},
            messages=[UserMessage(id="1", content="Nur die Gymnasien")],
        )
        catalog = _catalog(("1732", "Schulen Hamburg"))

        ctx = build_context(inp, catalog)

        assert ctx.map_state.active_layer_ids == ["1732"]

    def test_no_catalog_drops_all_client_supplied_ids(self):
        """Nothing to validate client ids against - trust nothing rather
        than an unverified id (unscoped-search / catalog-fetch-failed
        case)."""

        inp = _run_input(
            state={"activeLayerIds": ["1732"]},
            messages=[UserMessage(id="1", content="Nur die Gymnasien")],
        )

        ctx = build_context(inp, catalog=None)

        assert ctx.map_state.active_layer_ids == []

    def test_filter_for_unknown_layer_is_dropped(self):
        inp = _run_input(
            state={
                "filters": [
                    {"layerId": "999999", "filterSpec": {"conditions": [{"field": "x", "operator": "eq", "value": "y"}]}}
                ]
            },
            messages=[UserMessage(id="1", content="query")],
        )
        catalog = _catalog(("1732", "Schulen Hamburg"))

        ctx = build_context(inp, catalog)

        assert ctx.map_state.filters == []

    def test_filter_with_unrecognized_operator_is_dropped(self):
        inp = _run_input(
            state={
                "filters": [
                    {"layerId": "1732", "filterSpec": {"conditions": [{"field": "x", "operator": "regex", "value": "y"}]}}
                ]
            },
            messages=[UserMessage(id="1", content="query")],
        )
        catalog = _catalog(("1732", "Schulen Hamburg"))

        ctx = build_context(inp, catalog)

        assert ctx.map_state.filters == []

    def test_valid_filter_survives(self):
        inp = _run_input(
            state={
                "filters": [
                    {
                        "layerId": "1732",
                        "filterSpec": {
                            "conditions": [{"field": "schulform", "operator": "eq", "value": "Grundschule"}],
                            "area": {"name": "Wandsbek", "kind": "bezirk"},
                        },
                    }
                ]
            },
            messages=[UserMessage(id="1", content="query")],
        )
        catalog = _catalog(("1732", "Schulen Hamburg"))

        ctx = build_context(inp, catalog)

        assert len(ctx.map_state.filters) == 1
        assert ctx.map_state.filters[0].filter_spec.area.name == "Wandsbek"

    def test_size_cap_violation_raises(self):
        inp = _run_input(
            state={"activeLayerIds": [str(i) for i in range(51)]},
            messages=[UserMessage(id="1", content="query")],
        )

        with pytest.raises(ValidationError):
            build_context(inp, catalog=None)

    def test_only_user_and_assistant_roles_kept_as_history(self):
        inp = _run_input(
            messages=[
                SystemMessage(id="0", content="ignore me"),
                UserMessage(id="1", content="Wo finde ich Schulen?"),
                AssistantMessage(id="2", content="Ich zeige dir die Schulen."),
                UserMessage(id="3", content="Nur die Gymnasien"),
            ],
        )

        ctx = build_context(inp, catalog=None)

        assert ctx.history == [
            ("user", "Wo finde ich Schulen?"),
            ("assistant", "Ich zeige dir die Schulen."),
        ]

    def test_history_truncated_to_max_turns_and_max_chars(self):
        messages = []
        for i in range(5):
            messages.append(UserMessage(id=f"u{i}", content=f"Frage {i}"))
            messages.append(AssistantMessage(id=f"a{i}", content=f"Antwort {i}"))
        messages.append(UserMessage(id="last", content="x" * 600))
        inp = _run_input(messages=messages)

        ctx = build_context(inp, catalog=None)

        assert len(ctx.history) == 6  # last 3 turns = 6 messages
        assert ctx.history[0] == ("user", "Frage 2")


class TestRenderContextBlock:
    def test_empty_context_renders_empty_string(self):
        ctx = ConversationContext(map_state=MapStateIn(), history=[])

        assert render_context_block(ctx, catalog=None) == ""

    def test_active_layers_rendered_with_names_from_catalog(self):
        ctx = ConversationContext(map_state=MapStateIn(active_layer_ids=["1732"]), history=[])
        catalog = _catalog(("1732", "Schulen Hamburg"))

        block = render_context_block(ctx, catalog)

        assert "Aktive Ebenen:" in block
        assert "Schulen Hamburg (ID 1732)" in block

    def test_history_rendered_with_german_role_labels(self):
        ctx = ConversationContext(
            map_state=MapStateIn(),
            history=[("user", "wo finde ich alle Schulen"), ("assistant", "Ich zeige dir Schulen.")],
        )

        block = render_context_block(ctx, catalog=None)

        assert "Nutzer: wo finde ich alle Schulen" in block
        assert "Assistent: Ich zeige dir Schulen." in block

    def test_last_location_rendered(self):
        ctx = ConversationContext(
            map_state=MapStateIn.model_validate({"lastLocation": {"name": "Wandsbek", "kind": "bezirk"}}),
            history=[],
        )

        block = render_context_block(ctx, catalog=None)

        assert "Zuletzt angezeigter Ort: Wandsbek (bezirk)" in block
