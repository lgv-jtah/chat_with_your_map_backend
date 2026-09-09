"""Opt-in end-to-end test against the real Qwen3/vLLM server.

Skipped by default so ``pytest`` stays fast, deterministic and
network-independent; set ``CWYM_LIVE_TESTS=1`` (and ensure the server at
``CWYM_QWEN3_BASE_URL``/the default address is reachable) to run it,
e.g. when validating a new model deployment or after changing the
prompts in ``Qwen3Client``.
"""

import os

import pytest

from cwym_agent.llm.qwen3_client import Qwen3Client
from cwym_agent.settings import Qwen3Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("CWYM_LIVE_TESTS") != "1",
    reason="Set CWYM_LIVE_TESTS=1 to run tests against the real Qwen3/vLLM server.",
)


@pytest.fixture
def client() -> Qwen3Client:
    return Qwen3Client(Qwen3Settings())


def test_plan_resolves_location_and_layer_tools(client: Qwen3Client):
    reasoning_chunks: list[str] = []

    result = client.plan(
        "Zeige mir Fahrradstationen in Altona",
        streaming_callback=lambda chunk: reasoning_chunks.append(chunk.content),
    )

    assert "".join(reasoning_chunks).strip() != ""
    agent_names = {step["agent_name"] for step in result["steps"]}
    assert agent_names, "expected at least one sub-agent to be called"
    assert agent_names <= {"location_extractor", "layer_resolver", "action_detector"}


def test_generate_produces_non_empty_german_text(client: Qwen3Client):
    content_chunks: list[str] = []

    result = client.generate(
        "Nutzeranfrage: \"Zeige mir Fahrradstationen in Altona\"\n"
        "Aufgelöste Informationen: Ort: Altona; Kartenebene: StadtRAD-Stationen Hamburg\n"
        "Formuliere eine kurze, natürliche deutsche Antwort, die dem "
        "Nutzer mitteilt, was auf der Karte angezeigt wird.",
        streaming_callback=lambda chunk: content_chunks.append(chunk.content),
    )

    assert result.strip() != ""
    assert "".join(content_chunks) == result
